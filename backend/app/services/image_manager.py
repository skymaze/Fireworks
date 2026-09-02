"""镜像分发编排：单次 registry 下载 + Agent 间高速直传。

1. pulling/packing : 控制平面从 registry 流式拉取并组装 docker-archive
2. sending : 管理网发送 head（agent 反向拉取，GET 流式，断点续传）
3. syncing : worker Agent 经 RoCE/高速网从 head Agent 并行回拉（无 SSH/rsync）
4. loading : 各节点 docker load（已有同 digest 镜像自动跳过）+ digest 校验

各阶段幂等可续传。解决多节点同时向公网拉镜像的带宽竞争/网络不稳定问题。
同一 tag 的新构建（tag 漂移）在复用缓存归档时自动识别并重拉。
"""

import asyncio
import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from pathlib import Path

from ..db import SessionLocal
from ..background_tasks import spawn
from ..models import ImageTransfer, Node, iso_utc
from . import agent_client, peer_transfer

logger = logging.getLogger(__name__)
POLL_INTERVAL = 5

IMAGE_CACHE_DIR = Path(os.environ.get("IMAGE_CACHE_DIR", "./images-cache"))
# 参考模型分发：registry 层并行下载数（多核/多连接提速）
PULL_LAYER_WORKERS = int(os.environ.get("IMAGE_PULL_LAYER_WORKERS", "4"))
# 归档组装时并行解压的层数（gzip/zstd 解压为 CPU 密集，多核并行后顺序写 tar）
PACKING_WORKERS = int(os.environ.get("IMAGE_PACKING_WORKERS", "4"))


def image_archive_path(image: str, digest: str | None = None) -> Path:
    """控制平面镜像归档文件路径（docker-archive tar）。

    文件名固定用镜像名哈希（digest 拉取前可能未知，避免路径漂移）。
    """
    safe = hashlib.sha256(image.encode()).hexdigest()[:24]
    return IMAGE_CACHE_DIR / f"{safe}.tar"


# ---------- Registry 客户端（唯一下载实现，代理与进度行为一致） ----------


def _parse_image(image: str) -> tuple[str, str, str]:
    """解析镜像名为 (host, repo_path, tag)。docker.io 为默认 registry。"""
    name, sep, tag = image.rpartition(":")
    if not sep or "/" not in name and ":" in name:
        name, tag = image, "latest"
    if "/" not in name:
        host, path = "registry-1.docker.io", f"library/{name}"
    else:
        first, _, rest = name.partition("/")
        if "." in first or ":" in first or first == "localhost":
            host, path = first, rest
        else:
            host, path = "registry-1.docker.io", name
    return host, path, tag or "latest"


def _token_from_challenge(client: httpx.Client, challenge: str) -> str:
    """从 WWW-Authenticate 挑战头获取 Bearer token（ghcr/docker hub 通用）。"""
    if not challenge or "Bearer" not in challenge:
        return ""
    import re as _re

    realm = _re.search(r'realm="([^"]+)"', challenge)
    service = _re.search(r'service="([^"]+)"', challenge)
    scope = _re.search(r'scope="([^"]+)"', challenge)
    if not realm:
        return ""
    params = {}
    if service:
        params["service"] = service.group(1)
    if scope:
        params["scope"] = scope.group(1)
    try:
        tr = client.get(realm.group(1), params=params, follow_redirects=True)
        return tr.json().get("token", "")
    except Exception:
        return ""


def _registry_token(client: httpx.Client, host: str, path: str) -> str:
    """registry 匿名 token（401 -> Bearer token 流程），公开仓库无需认证时返回空。"""
    try:
        r = client.get(f"https://{host}/v2/", follow_redirects=True)
    except Exception:
        return ""
    if r.status_code != 401:
        return ""
    return _token_from_challenge(client, r.headers.get("www-authenticate", ""))


def _registry_manifest(client: httpx.Client, host: str, path: str, tag: str,
                       token: str) -> tuple[dict, str]:
    """获取 arm64 镜像 manifest（manifest list 选 linux/arm64），返回 (manifest, digest)。"""
    accept = ", ".join([
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    ])
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://{host}/v2/{path}/manifests/{tag}"
    r = client.get(url, headers=headers, follow_redirects=True)
    if r.status_code == 401:
        # ghcr 等在 manifests 请求上发起认证挑战
        token2 = _token_from_challenge(client, r.headers.get("www-authenticate", ""))
        if token2:
            token = token2
            headers["Authorization"] = f"Bearer {token}"
            r = client.get(url, headers=headers, follow_redirects=True)
    if r.status_code == 404:
        raise RuntimeError(f"镜像不存在: {image_name(host, path, tag)}")
    r.raise_for_status()
    data = r.json()
    digest = r.headers.get("docker-content-digest", "")
    if not digest and r.status_code == 200:
        digest = data.get("digest", "")
    # manifest list：选 linux/arm64
    manifests = data.get("manifests")
    if manifests:
        target = next(
            (m for m in manifests
             if (m.get("platform") or {}).get("os") == "linux"
             and (m.get("platform") or {}).get("architecture") in ("arm64", "aarch64")),
            None,
        )
        if target is None:
            raise ValueError("镜像不支持 linux/arm64，无法用于 DGX Spark")
        digest = target.get("digest", digest)
        headers = {"Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r2 = client.get(f"https://{host}/v2/{path}/manifests/{digest}",
                        headers=headers, follow_redirects=True)
        r2.raise_for_status()
        data = r2.json()
    return data, digest


def image_name(host: str, path: str, tag: str) -> str:
    prefix = "" if host == "registry-1.docker.io" else f"{host}/"
    return f"{prefix}{path}:{tag}"


def _registry_blob(client: httpx.Client, host: str, path: str, digest: str,
                   token: str) -> bytes:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"https://{host}/v2/{path}/blobs/{digest}"
    r = client.get(url, headers=headers, follow_redirects=True, timeout=3600)
    if r.status_code == 401:
        token2 = _token_from_challenge(client, r.headers.get("www-authenticate", ""))
        if token2:
            r = client.get(url, headers={"Authorization": f"Bearer {token2}"},
                           follow_redirects=True, timeout=3600)
    r.raise_for_status()
    return r.content


_RETRYABLE = (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError,
              httpx.ReadError, httpx.NetworkError, httpx.StreamError)


def _registry_blob_file(client: httpx.Client, host: str, path: str, digest: str,
                        token: str, dest: Path, expect_size: int | None = None,
                        progress: Callable[[int], None] | None = None) -> None:
    """流式下载 registry blob 到文件：Range 断点续传 + 连接中断重试 + sha256 校验。

    大镜像层（可达 GB 级）经代理传输易中断（peer closed / timeout）：
    - 流式落盘 .part，避免整块加载内存；
    - 中断后用 Range: bytes=N- 从断点续传（服务器不支持 Range 时返回 200，从头重下）；
    - 连接类错误最多重试 5 次，token 失效（401）时重新获取；
    - 完成后 sha256 校验（续传时纳入已下载字节）；
    - Range 越界（416）时 .part 已完整，先校验内容再收尾落盘，避免重复撞 416 卡死。
    """
    if dest.exists():
        size_ok = not expect_size or dest.stat().st_size == expect_size
        if size_ok and _cached_archive_digest(dest) == digest:
            # sidecar 命中：已校验完整的 blob，跳过下载且不再整份重读
            if progress:
                progress(dest.stat().st_size)
            return
        hash_ok = not digest.startswith("sha256:") or _archive_fingerprint(dest) == digest
        if size_ok and hash_ok:
            _mark_archive_digest(dest, digest)
            if progress:
                progress(dest.stat().st_size)
            return
        dest.unlink(missing_ok=True)
    url = f"https://{host}/v2/{path}/blobs/{digest}"
    tmp = dest.with_name(dest.name + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    attempts = 0
    while True:
        have = tmp.stat().st_size if tmp.exists() else 0
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with client.stream("GET", url, headers=headers, follow_redirects=True,
                               timeout=httpx.Timeout(3600, connect=60)) as r:
                if r.status_code == 401:
                    attempts += 1
                    if attempts >= 5:
                        raise RuntimeError(
                            f"blob 下载持续返回 401（registry 鉴权失败），已重试 {attempts} 次")
                    token2 = _token_from_challenge(client, r.headers.get("www-authenticate", ""))
                    if not token2:
                        r.raise_for_status()
                    token = token2
                    continue  # 换 token 后重试（Range 续传，保留已下载分片）
                if r.status_code == 416:
                    # Range 越界 = .part 已下载完整（或已超长）。校验已下载内容，
                    # 通过则 rename 收尾；大小不符/哈希失败/超长则删除重下。
                    size_ok = (not expect_size
                               or (tmp.exists() and tmp.stat().st_size == expect_size))
                    if size_ok and tmp.exists() and tmp.stat().st_size > 0:
                        h = hashlib.sha256()
                        with open(tmp, "rb") as f:
                            for chunk in iter(lambda: f.read(1 << 20), b""):
                                h.update(chunk)
                        got = "sha256:" + h.hexdigest()
                        if not digest.startswith("sha256:") or got == digest:
                            tmp.rename(dest)
                            _mark_archive_digest(dest, digest)
                            if progress:
                                progress(dest.stat().st_size)
                            return
                    tmp.unlink(missing_ok=True)
                    attempts += 1
                    if attempts >= 5:
                        raise RuntimeError(
                            f"blob 下载持续返回 416（服务器端文件不完整），已重试 {attempts} 次")
                    continue
                if have and r.status_code == 200:
                    # 服务器不支持 Range：从头重下
                    tmp.unlink(missing_ok=True)
                    continue
                r.raise_for_status()
                h = hashlib.sha256()
                if have:
                    # 续传：sha256 必须包含已下载字节
                    with open(tmp, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
                with open(tmp, "ab" if have else "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
                        have += len(chunk)
                        if progress:
                            progress(have)
                if expect_size and tmp.stat().st_size != expect_size:
                    raise RuntimeError(
                        f"blob 大小不符: {tmp.stat().st_size} != {expect_size}")
                got = "sha256:" + h.hexdigest()
                if digest.startswith("sha256:") and got != digest:
                    raise RuntimeError(
                        f"blob sha256 校验失败: {got[:16]} != {digest[:16]}")
                tmp.rename(dest)
                _mark_archive_digest(dest, digest)
                return
        except _RETRYABLE as e:
            attempts += 1
            logger.warning("blob 下载中断（第 %d/5 次）%s: %s",
                           attempts, digest[:16], str(e)[:120])
            if attempts >= 5:
                raise
        except httpx.HTTPStatusError:
            tmp.unlink(missing_ok=True)
            raise


def _build_docker_archive(image: str, manifest: dict, config_blob: bytes,
                          layer_files: list[tuple[str, Path]], dest: Path) -> None:
    """组装 docker-archive（docker save 格式）：manifest.json + config + 各层 layer.tar。

    registry 的 layer blob 是压缩 tar（gzip / zstd），需解压为 plain tar
    （docker load 格式）。压缩格式按 blob 魔数识别，而非依赖 mediaType：
    - gzip（1f 8b）与 zstd（28 b5 2f fd）解压为 plain tar；
    - 其余视为已解压 tar 原样使用（bzip2/xz 等罕见层保留压缩，由 docker 处理）。

    各层先在临时线程池**并行解压**落盘（gzip/zstd 解压是 CPU 密集，多核提速），
    再按 manifest 顺序流式写入 tar——顺序与字节不变，输出指纹稳定。
    .plain 临时文件名绑定本次归档目标，避免并发任务共享同一 blob 时互相覆盖。
    """
    import tarfile

    import zstandard

    cfg_digest = (manifest.get("config") or {}).get("digest", "sha256:0")
    cfg_name = cfg_digest.replace("sha256:", "") + ".json"
    layer_names = []
    for ld, _p in layer_files:
        layer_names.append(f"{ld.replace('sha256:', '')}/layer.tar")
    manifest_entry = [{
        "Config": cfg_name,
        "RepoTags": [image],
        "Layers": layer_names,
    }]
    suffix = f".{dest.stem}"  # 绑定本次归档，隔离并发任务的临时文件

    def _prepare_one(ld: str, p: Path):
        """解压单个层；返回 (目标源路径, 是否为需清理的临时 plain)。"""
        with open(p, "rb") as f:
            magic = f.read(4)
        if magic[:2] == b"\x1f\x8b":
            # gzip 层（绝大多数 registry 层）
            plain = p.with_name(f"{ld.replace('sha256:', '')[:16]}{suffix}.plain")
            with gzip.open(p, "rb") as gz, open(plain, "wb") as pf:
                shutil.copyfileobj(gz, pf, 1 << 20)
            return plain, True
        if magic[:4] == b"\x28\xb5\x2f\xfd":
            # zstd 层（buildah/podman 构建的镜像）：不解压会以 zstd 字节
            # 冒充 layer.tar，docker load 报 archive/tar: invalid tar header
            plain = p.with_name(f"{ld.replace('sha256:', '')[:16]}{suffix}.plain")
            with open(p, "rb") as zf, open(plain, "wb") as pf:
                zstandard.ZstdDecompressor().copy_stream(zf, pf, 1 << 20)
            return plain, True
        return p, False

    prepared: dict[str, tuple[Path, bool]] = {}
    try:
        with ThreadPoolExecutor(
            max_workers=min(PACKING_WORKERS, max(1, len(layer_files)))
        ) as ex:
            futures = {
                ex.submit(_prepare_one, ld, p): ld
                for ld, p in layer_files
            }
            for fut in as_completed(futures):
                src, volatile = fut.result()
                prepared[futures[fut]] = (src, volatile)
        with tarfile.open(dest, "w") as out:
            mj = io.BytesIO(json.dumps(manifest_entry).encode())
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(mj.getvalue())
            out.addfile(ti, mj)
            # config
            ci = io.BytesIO(config_blob)
            ti2 = tarfile.TarInfo(cfg_name)
            ti2.size = len(config_blob)
            out.addfile(ti2, ci)
            # layers（已解压 plain，按 manifest 顺序流式写入）
            for (ld, _p), name in zip(layer_files, layer_names):
                src, _volatile = prepared[ld]
                ti3 = tarfile.TarInfo(name)
                ti3.size = src.stat().st_size
                with open(src, "rb") as lf:
                    out.addfile(ti3, lf)
    finally:
        for src, volatile in prepared.values():
            if volatile:
                src.unlink(missing_ok=True)


def _pull_via_registry(
    image: str,
    dest: Path,
    proxy: str | None,
    progress: Callable[[int, int], None] | None = None,
    phase: Callable[[str], None] | None = None,
) -> str:
    """Python registry API 拉取镜像（强制 linux/arm64，支持代理）。

    大层流式落盘 + Range 断点续传 + 连接中断重试（代理传输不稳定时的容错）；
    各层并行下载（多核/多连接提速，参考模型分发的并发思路）。
    """
    host, path, tag = _parse_image(image)
    client_kwargs = {"timeout": 120}
    if proxy:
        client_kwargs["proxy"] = proxy
    with httpx.Client(**client_kwargs) as client:
        token = _registry_token(client, host, path)
        manifest, digest = _registry_manifest(client, host, path, tag, token)
        # 架构校验（manifest list 已强制 arm64；单架构校验 config）
        cfg_digest = (manifest.get("config") or {}).get("digest", "sha256:0")
        cfg_blob = _registry_blob(client, host, path, cfg_digest, token)
        cfg = json.loads(cfg_blob) if cfg_blob else {}
        arch, os_name = cfg.get("architecture", ""), cfg.get("os", "")
        if arch and os_name and (os_name != "linux" or arch not in ("arm64", "aarch64")):
            raise ValueError(f"镜像平台 {os_name}/{arch} 不适用于 DGX Spark（需要 linux/arm64）")
        # 下载 layers（持久 blob 缓存：已校验完成的层复用，跨任务不重复下载）
        layers = [m for m in manifest.get("layers", []) if m.get("digest")]
        total = sum(int(m.get("size") or 0) for m in layers)
        blob_dir = IMAGE_CACHE_DIR / ".blobs"
        blob_dir.mkdir(parents=True, exist_ok=True)

        class _PullTracker:
            """线程安全累计进度：每层开始前取锚点，完成后累加整层字节。"""

            def __init__(self, report: Callable[[int, int], None] | None,
                         total_bytes: int):
                self._report = report
                self._total = total_bytes
                self._lock = threading.Lock()
                self._done = 0

            def anchor(self):
                """返回该层下载进度回调（锚定到当前已完成字节，并发时近似）。"""
                if not self._report:
                    return None
                with self._lock:
                    base = self._done
                return lambda current, _t=None: self._report(base + current, self._total)

            def complete(self, size: int) -> None:
                with self._lock:
                    self._done += size
                    done = self._done
                if self._report:
                    self._report(done, self._total)

        tracker = _PullTracker(progress, total)

        def _pull_layer(layer: dict):
            ld = layer["digest"]
            lp = blob_dir / ld.replace("sha256:", "")[:24]
            size = int(layer.get("size") or 0)
            _registry_blob_file(
                client, host, path, ld, token, lp,
                expect_size=size or None, progress=tracker.anchor(),
            )
            return ld, lp, size

        by_digest: dict[str, tuple[str, Path]] = {}
        with ThreadPoolExecutor(
            max_workers=min(PULL_LAYER_WORKERS, max(1, len(layers)))
        ) as ex:
            futures = {ex.submit(_pull_layer, layer): layer for layer in layers}
            for fut in as_completed(futures):
                ld, lp, size = fut.result()  # 任一失败在此抛出 -> 任务失败（同串行语义）
                by_digest[ld] = (ld, lp)
                tracker.complete(size or (lp.stat().st_size if lp.exists() else 0))
        # 按 manifest 顺序组装（docker-archive 的 Layers 顺序必须与 manifest 一致）
        layer_files = [by_digest[layer["digest"]] for layer in layers]
        if phase:
            phase("packing")
        _build_docker_archive(image, manifest, cfg_blob, layer_files, dest)
    return digest


def _inspect_via_registry(image: str, proxy: str | None) -> dict:
    """Python registry API 查询镜像元数据（arm64 视图）。"""
    host, path, tag = _parse_image(image)
    client_kwargs = {"timeout": 120}
    if proxy:
        client_kwargs["proxy"] = proxy
    with httpx.Client(**client_kwargs) as client:
        token = _registry_token(client, host, path)
        manifest, digest = _registry_manifest(client, host, path, tag, token)
        layers = manifest.get("layers", [])
        size = sum(l.get("size") or 0 for l in layers)
        # config 里的架构
        arch = os_name = ""
        try:
            cfg_digest = (manifest.get("config") or {}).get("digest")
            if cfg_digest:
                cfg = json.loads(_registry_blob(client, host, path, cfg_digest, token))
                arch, os_name = cfg.get("architecture", ""), cfg.get("os", "")
        except Exception:
            pass
        return {
            "image": image,
            "digest": digest if digest.startswith("sha256:") else f"sha256:{digest}",
            "size_bytes": size,
            "layers": len(layers),
            "arch": arch or "arm64",
            "os": os_name or "linux",
        }


def _proxy_value() -> str | None:
    from .model_manager import get_hf_settings

    v = (get_hf_settings().get("docker_proxy") or "").strip()
    return v or None


def inspect_image(image: str) -> dict:
    """用与下载相同的 registry 客户端查询 linux/arm64 镜像元数据。"""
    return _inspect_via_registry(image, _proxy_value())


def _archive_fingerprint(dest: Path) -> str:
    """归档文件 sha256 指纹（传输任务 digest 基准）。

    归档构建是确定性的（固定顺序 + 解压字节 + mtime=0），同一镜像的归档
    字节跨节点一致。registry 的 digest（index / 子 manifest）与 docker 侧
    RepoDigests/Id 在 containerd 与传统存储模式下行为不一致，无法作为
    跨环境校验基准，故统一以归档文件指纹作为 digest。
    """
    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _archive_digest_marker(dest: Path) -> Path:
    """缓存归档/blob 的 known-digest sidecar（避免重复整份重算）。"""
    return dest.with_name(dest.name + ".digest")


def _mark_archive_digest(dest: Path, digest: str,
                         registry_digest: str | None = None) -> None:
    """记录文件内容指纹 sidecar；校验绑定 mtime+size，文件一旦被替换即失效。

    registry_digest（可选）记录「产生该归档的 registry 内容 digest」，供
    同 tag 新构建（tag 漂移）检测；blob 级 sidecar 不传该字段。
    """
    stat = dest.stat()
    info: dict = {
        "digest": digest,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if registry_digest:
        info["registry_digest"] = registry_digest
    _archive_digest_marker(dest).write_text(json.dumps(info), encoding="utf-8")


def _archive_registry_digest(dest: Path) -> str | None:
    """读取归档 sidecar 记录的 registry digest；缺失/损坏返回 None。

    None 有两重含义：旧版归档（升级前产生的 sidecar 无该字段）或从未记录，
    调用方按「未知版本」保守处理。
    """
    try:
        info = json.loads(_archive_digest_marker(dest).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    v = info.get("registry_digest")
    return v if isinstance(v, str) and v.startswith("sha256:") else None


def archive_registry_digest_for(image: str) -> str | None:
    """按镜像名取控制平面归档对应的 registry 内容 digest（展示用）。"""
    return _archive_registry_digest(image_archive_path(image))


def _cached_archive_digest(dest: Path) -> str | None:
    """读取缓存文件的已知指纹；缺失/损坏/尺寸或 mtime 不符返回 None，由调用方重算。"""
    try:
        info = json.loads(_archive_digest_marker(dest).read_text(encoding="utf-8"))
        stat = dest.stat()
        if (info.get("digest", "").startswith("sha256:")
                and info.get("size") == stat.st_size
                and info.get("mtime_ns") == stat.st_mtime_ns):
            return info["digest"]
    except (OSError, ValueError, TypeError):
        pass
    return None


def pull_image(
    image: str,
    dest: Path,
    progress: Callable[[int, int], None] | None = None,
    phase: Callable[[str], None] | None = None,
) -> None:
    """用唯一 registry 路径拉取镜像，支持代理、断点续传和逐层进度。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        _pull_via_registry(image, tmp, _proxy_value(), progress=progress, phase=phase)
        tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)


def image_transfer_to_dict(t: ImageTransfer) -> dict:
    return {
        "id": t.id,
        "image": t.image,
        "digest": t.digest,
        "registry_digest": t.registry_digest,
        "head_node_id": t.head_node_id,
        "status": t.status,
        "downloaded_bytes": t.downloaded_bytes,
        "sent_bytes": t.sent_bytes,
        "size_bytes": t.size_bytes,
        "sync_jobs": t.sync_jobs,
        "error": t.error,
        "created_at": iso_utc(t.created_at),
    }


# ---------- 跨种类并发下载互斥（模型 <-> 镜像） ----------
#
# 与控制平面模型下载对称：同一时间只允许一个外部下载源，模型 HF 拉取与镜像
# registry 拉取互斥；已缓存的资源只分发不拉取，可与对方分发并发进行。


def _reject_if_model_downloading() -> None:
    """有模型正在下载时拒绝开始镜像拉取（调用方把 ValueError 转为 409）。

    「正在下载」按控制平面目标版本是否就绪判定：状态为 downloading 但目标版本
    已完整的任务只是分发前的快速跳过，不构成并发下载。用本模块 SessionLocal
    查询（与调用方同一数据库会话源），就绪判定借用 model_manager 的纯函数。
    """
    from ..models import ModelDownload
    from .model_manager import _model_job_target_ready

    db = SessionLocal()
    try:
        downs = db.query(ModelDownload).filter(
            ModelDownload.status == "downloading",
        ).all()
    finally:
        db.close()
    for j in downs:
        if _model_job_target_ready(j):
            continue  # 目标版本已就绪：分发前快速跳过，不构成并发下载
        raise ValueError(
            f"模型 {j.repo} 正在下载（任务 #{j.id}），不能与镜像同时下载；"
            "请等待其完成或取消后再拉取镜像")


# ---------- 阶段状态机 ----------

_ACTIVE_STATUSES = ("pulling", "packing", "sending", "syncing", "loading")
# 进行中的拉取线程（拉取为子进程无法中途终止，仅供暂停/继续时判断是否已有线程在跑）
_pull_threads: dict[int, threading.Thread] = {}
# 暂停时记录原阶段，继续时回到该阶段
_paused_phase: dict[int, str] = {}


async def pause_image_transfer(job_id: int) -> dict:
    """暂停镜像传输：发送/同步/加载阶段即刻停；拉取阶段标记暂停（子进程拉完归档后停在发送前）。"""
    db = SessionLocal()
    try:
        t = db.get(ImageTransfer, job_id)
        if not t:
            raise ValueError("镜像传输任务不存在")
        if t.status not in _ACTIVE_STATUSES:
            raise ValueError(f"任务当前状态 {t.status}，无法暂停")
        _paused_phase[job_id] = t.status
        t.status = "paused"
        t.error = None
        db.commit()
        return image_transfer_to_dict(t)
    finally:
        db.close()


async def resume_image_transfer(job_id: int) -> dict:
    """继续暂停的传输：回到原阶段；拉取阶段归档未就绪且无在跑线程时重启拉取。"""
    db = SessionLocal()
    try:
        t = db.get(ImageTransfer, job_id)
        if not t:
            raise ValueError("镜像传输任务不存在")
        if t.status != "paused":
            raise ValueError(f"任务当前状态 {t.status}，无法继续")
        dest = image_archive_path(t.image, t.digest)
        pull_complete = (
            dest.exists() and dest.stat().st_size > 0
            and t.downloaded_bytes == dest.stat().st_size
        )
        # 后端重启会丢失内存中的暂停阶段；用归档完成状态恢复，不能把尚未完成
        # 的 pulling/packing 误恢复到 sending。
        phase = _paused_phase.pop(job_id, "sending" if pull_complete else "pulling")
        # 继续到拉取/打包阶段且归档未落盘会重启外部拉取：遵守跨种类并发下载互斥，
        # 在写回 pulling/packing 状态前检查，拒绝时不留下「无线程的拉取中」任务。
        if phase in ("pulling", "packing"):
            res_dest = image_archive_path(t.image, t.digest)
            if not (res_dest.exists() and res_dest.stat().st_size > 0):
                _reject_if_model_downloading()
        t.status = phase
        t.error = None
        db.commit()
        if phase in ("pulling", "packing"):
            pt = _pull_threads.get(job_id)
            if not pt or not pt.is_alive():
                threading.Thread(target=_start_pull, args=(t.id, False), daemon=True).start()
        spawn(_monitor_transfer(job_id))
        return image_transfer_to_dict(t)
    finally:
        db.close()


async def cancel_image_transfer(job_id: int) -> dict:
    """取消镜像传输：标记 cancelled；拉取子进程无法中途终止，归档完成后作废（缓存复用）。"""
    db = SessionLocal()
    try:
        t = db.get(ImageTransfer, job_id)
        if not t:
            raise ValueError("镜像传输任务不存在")
        if t.status not in _ACTIVE_STATUSES + ("paused",):
            raise ValueError(f"任务当前状态 {t.status}，无法取消")
        _paused_phase.pop(job_id, None)
        t.status = "cancelled"
        t.error = "用户取消"
        db.commit()
        return image_transfer_to_dict(t)
    finally:
        db.close()


async def start_image_transfer(image: str, head_node_id: int | None,
                               sync_node_ids: list[int], force: bool = False) -> ImageTransfer:
    """创建镜像传输任务。head_node_id 为 None 时仅下载到控制平面。

    force=True 强制重新拉取（覆盖已有归档，刷新最新版本）。
    """
    db = SessionLocal()
    try:
        active = db.query(ImageTransfer).filter(
            ImageTransfer.image == image,
            ImageTransfer.status.in_([
                "pulling", "packing", "sending", "syncing", "loading", "paused",
            ]),
        ).first()
        if active:
            raise ValueError(f"该镜像已有进行中的传输任务 #{active.id}（{active.status}）")
        dest = image_archive_path(image)
        info = None
        try:
            info = inspect_image(image)
        except Exception as e:
            # registry 不可达/受限时，只要控制平面已有该镜像的缓存归档，仍可继续
            # 部署分发（head/worker 只依赖归档内容，不依赖 registry）；仅当既无
            # 归档也无 registry 时才把检查失败上报给用户。
            if not (dest.exists() and dest.stat().st_size > 0):
                raise
            logger.warning("registry 检查失败（%s），改用缓存归档分发: %s",
                           str(e)[:120], image)
        if info is None:
            digest, size_bytes = "", dest.stat().st_size
        else:
            digest, size_bytes = info["digest"], info["size_bytes"]
        # 跨种类互斥：仅当本次需要真实拉取（归档缺失，或 tag 漂移将自动重拉）时，
        # 禁止与进行中的模型下载并发；已缓存的镜像只分发不拉取，可与模型分发并发。
        needs_pull = force or not (dest.exists() and dest.stat().st_size > 0)
        if not needs_pull and info and info.get("digest"):
            if _archive_registry_digest(dest) != info["digest"]:
                needs_pull = True
        if needs_pull:
            _reject_if_model_downloading()
        t = ImageTransfer(
            image=image,
            digest=digest,
            registry_digest=digest or None,   # inspect 到的 registry 内容 digest（""=registry 不可达回退）
            head_node_id=head_node_id,
            status="pulling",
            size_bytes=size_bytes,
            sync_jobs={str(nid): {"status": "pending"} for nid in sync_node_ids},
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        threading.Thread(target=_start_pull, args=(t.id, force), daemon=True).start()
        spawn(_monitor_transfer(t.id))
        return t
    finally:
        db.close()


async def ensure_image_on_nodes(image: str, nodes: list, head_node_id: int | None) -> dict:
    """任务发布前保障镜像已分发到各节点（缺失则启动管理传输，不阻塞等待）。

    返回 {"ok": bool, "missing": [...], "message": str}
    与模型路径一致：镜像缺失时启动传输后立即返回 ok=False，由前端轮询就绪后重新发布，
    避免单次慢传输挂起请求线程数小时。
    """
    from .agent_client import image_status

    missing = []
    for n in nodes:
        try:
            st = await image_status(n, image)
            # 发布前就绪判定：present（docker 中已有该 tag）即算就绪，避免每次发布都触发
            # 大镜像传输。版本陈旧/缺失由传输流程保证：monitor 阶段 4 会对每个节点执行
            # image_load（归档指纹标记，无标记即 load 新版本）。
            if not st.get("present"):
                missing.append(n.name)
        except Exception:
            missing.append(f"{n.name}（agent 不可达或版本过旧，请重新部署 Agent）")
    if not missing:
        return {"ok": True, "missing": [], "message": "镜像已就绪（全部节点已加载）"}

    sync_ids = [n.id for n in nodes if n.id != head_node_id]
    t = await start_image_transfer(image, head_node_id, sync_ids, force=False)
    return {
        "ok": False,
        "missing": missing,
        "message": f"镜像传输已启动（任务 #{t.id}），完成后请重新发布",
    }


def _start_pull(job_id: int, force: bool = False) -> None:
    """阶段 1：控制平面 registry 拉取并组装 docker-archive（线程）。

    force=True 时忽略已有归档强制重新拉取（刷新最新版本）。
    注册到 _pull_threads 供暂停/继续查询；拉取为子进程无法中途终止，
    暂停/取消只标记状态，归档完成后由监控流程停在后续阶段。
    """
    _t = threading.current_thread()
    _pull_threads[job_id] = _t
    db = SessionLocal()
    # 并行层下载会从多个工作线程回调进度；同一 Session 的 refresh/commit 必须
    # 串行化，否则并发 commit 触发 SQLAlchemy 状态竞争（InvalidRequestError:
    # "Method 'commit()' can't be called here..."），随机中断拉取并使任务卡死。
    db_lock = threading.Lock()
    try:
        t = db.get(ImageTransfer, job_id)
        if not t:
            return
        dest = image_archive_path(t.image, t.digest)
        # 该 tag 创建任务时 inspect 到的 registry 内容 digest（行内持久化，重启/恢复后仍可检测）
        current_registry = t.registry_digest or ""
        if force and dest.exists():
            dest.unlink(missing_ok=True)
            _archive_digest_marker(dest).unlink(missing_ok=True)
        # 同 tag 新构建（tag 漂移）自动重拉：缓存归档的 registry digest 与当前不一致。
        # 归档无记录/无 sidecar（升级前产物、版本未知）也保守视为漂移，重拉一次并补记。
        # registry 不可达（current_registry 为空）时保持原兜底，用缓存归档分发。
        if not force and current_registry and dest.exists() and dest.stat().st_size > 0:
            if _archive_registry_digest(dest) != current_registry:
                logger.info("镜像 tag 已指向新构建，自动重新拉取: %s (%s…)",
                            t.image, current_registry[:16])
                dest.unlink(missing_ok=True)
                _archive_digest_marker(dest).unlink(missing_ok=True)
        if not (dest.exists() and dest.stat().st_size > 0):
            last_commit = 0.0

            def on_progress(done: int, total: int) -> None:
                nonlocal last_commit
                now = time.monotonic()
                if now - last_commit < 0.5 and done < total:
                    return
                with db_lock:
                    db.refresh(t)
                    if t.status != "pulling":
                        return
                    t.downloaded_bytes = done
                    if total:
                        t.size_bytes = total
                    db.commit()
                last_commit = now

            def on_phase(phase: str) -> None:
                with db_lock:
                    db.refresh(t)
                    if t.status == "pulling":
                        t.status = phase
                        db.commit()

            pull_image(t.image, dest, progress=on_progress, phase=on_phase)
        # 统一 digest：归档文件 sha256 指纹（构建确定性，跨节点字节一致）。
        # 已缓存归档优先复用 sidecar 指纹；缺失/尺寸或 mtime 不符才全量重算
        # （避免每次重复分发都整份读一遍 GB 级归档）。已完整的最新归档不再
        # 触发任何 registry 拉取（见上方大小判断）。
        with db_lock:
            t.digest = _cached_archive_digest(dest)
            if not t.digest and dest.exists() and dest.stat().st_size > 0:
                t.digest = _archive_fingerprint(dest)
                _mark_archive_digest(dest, t.digest,
                                     registry_digest=current_registry or None)
            t.registry_digest = current_registry or None
            t.downloaded_bytes = dest.stat().st_size if dest.exists() else 0
            t.size_bytes = t.downloaded_bytes
            db.commit()
    except Exception as e:
        logger.warning("镜像拉取失败 job=%s: %s", job_id, e)
        try:
            with db_lock:
                db.rollback()
                t = db.get(ImageTransfer, job_id)
                if t and t.status in ("pulling", "packing", "paused"):
                    t.status = "failed"
                    t.error = f"拉取失败: {e}"
                    db.commit()
        except Exception as e2:
            # 主会话可能在并发写中残留坏状态（竞争极低频残留）。必须仍把任务
            # 标记为失败，否则会永远卡在 pulling、阻塞同一镜像的重试。
            logger.warning("会话异常，改用新会话标记拉取失败 job=%s: %s", job_id, e2)
            try:
                db2 = SessionLocal()
                try:
                    t2 = db2.get(ImageTransfer, job_id)
                    if t2 and t2.status in ("pulling", "packing", "paused"):
                        t2.status = "failed"
                        t2.error = f"拉取失败: {e}"
                        db2.commit()
                finally:
                    db2.close()
            except Exception as e3:
                logger.warning("新会话标记失败仍失败 job=%s: %s", job_id, e3)
    finally:
        if _pull_threads.get(job_id) is _t:
            _pull_threads.pop(job_id, None)
        db.close()


async def _monitor_transfer(job_id: int) -> None:
    db = SessionLocal()
    try:
        t = db.get(ImageTransfer, job_id)
        if not t:
            return
        head = db.get(Node, t.head_node_id) if t.head_node_id else None
        if t.head_node_id and not head:
            t.status = "failed"
            t.error = "head 节点不存在"
            db.commit()
            return

        # 阶段 1：等待下载线程完成。归档指纹只由该线程计算一次，避免监控线程
        # 在大文件落盘瞬间并发重复扫描整个归档。
        while t.status in ("pulling", "packing"):
            db.refresh(t)
            dest = image_archive_path(t.image, t.digest)
            pull_thread = _pull_threads.get(job_id)
            if (
                dest.exists()
                and dest.stat().st_size > 0
                and t.downloaded_bytes == dest.stat().st_size
                and (not pull_thread or not pull_thread.is_alive())
            ):
                break
            await asyncio.sleep(POLL_INTERVAL)
        if t.status in ("failed", "cancelled", "paused"):
            return  # 失败/用户取消/暂停：不再推进流程

        if head is None:
            # 仅下载到控制平面
            t.status = "completed"
            db.commit()
            return

        target_nodes = [head]
        for nid_str in (t.sync_jobs or {}):
            worker = db.get(Node, int(nid_str))
            if worker:
                target_nodes.append(worker)
        capability_errors = []
        for node in target_nodes:
            error = await peer_transfer.check_agent_capability(
                node, agent_client, "image_peer_transfer_v1",
            )
            if error:
                capability_errors.append(error)
        if capability_errors:
            t.status = "failed"
            t.error = "Agent 能力检查失败：" + "；".join(capability_errors)
            db.commit()
            return

        # 阶段 2：管理网发送 head（agent 反向拉取）
        t.status = "sending"
        t.sent_bytes = 0
        db.commit()
        try:
            dest = image_archive_path(t.image, t.digest)
            sent = await _send_archive_to_node(head, t, dest)
            t.sent_bytes = sent
            db.commit()
        except Exception as e:
            t.status = "failed"
            t.error = f"发送到 head 失败: {e}"
            db.commit()
            return
        db.refresh(t)
        if t.status != "sending":
            return  # 发送期间被暂停/取消

        # 阶段 3：各 worker Agent 经 RoCE/高速网并行从 head Agent 回拉。
        t.status = "syncing"
        db.commit()
        head_ip = peer_transfer.node_transfer_ip(db, head)
        try:
            share = await agent_client.image_share(head, t.digest or "")
            if int(share.get("size") or 0) != (t.size_bytes or 0):
                raise RuntimeError(
                    f"head 归档大小异常: {share.get('size')} != {t.size_bytes or 0}"
                )
            # 校验 head 返回的路径/令牌，防止注入 userinfo 把 worker 拉取重定向到外部主机
            share_path = peer_transfer.validate_share_path(share.get("path"))
            share_token = peer_transfer.validate_share_token(share.get("token"))
            source_url = f"http://{head_ip}:{head.agent_port}{share_path}"
        except Exception as e:
            t.status = "failed"
            t.error = f"head 开放高速传输失败: {e}"
            db.commit()
            return
        workers: list[Node] = []
        initial_jobs = dict(t.sync_jobs or {})
        for nid_str in initial_jobs:
            worker = db.get(Node, int(nid_str))
            if worker:
                workers.append(worker)
                initial_jobs[nid_str] = {
                    "status": "syncing", "transferred_bytes": 0,
                    "total_bytes": t.size_bytes or 0,
                }
            else:
                initial_jobs[nid_str] = {"status": "failed", "error": "worker 不存在"}
        t.sync_jobs = initial_jobs
        db.commit()
        results = await asyncio.gather(*[
            _sync_archive_to_worker(
                worker, t.id, t.image, t.digest or "", t.size_bytes or 0,
                source_url, share_token,
            )
            for worker in workers
        ])
        db.refresh(t)
        merged_jobs = dict(t.sync_jobs or {})
        for node_id, result in results:
            merged_jobs[str(node_id)] = result
        t.sync_jobs = merged_jobs
        db.commit()
        all_ok = all(j.get("status") == "completed" for j in merged_jobs.values())
        db.refresh(t)
        if t.status != "syncing":
            return
        if not all_ok:
            t.status = "failed"
            failed = [
                f"#{nid} {j.get('error') or '未知错误'}"[:300]
                for nid, j in (t.sync_jobs or {}).items()
                if j.get("status") == "failed"
            ]
            t.error = "部分 worker 同步失败" + (f"：{'；'.join(failed)}" if failed else "")
            db.commit()
            return

        # 阶段 4：head + 各 worker 节点 docker load（已有同 digest 跳过）。
        # 各节点加载相互独立，并行执行把总耗时从「各节点求和」降到「最慢节点」。
        t.status = "loading"
        db.commit()
        load_nodes: list[Node] = []
        for nid_str in ["head"] + list((t.sync_jobs or {}).keys()):
            node = head if nid_str == "head" else db.get(Node, int(nid_str))
            if node:
                load_nodes.append(node)
        if load_nodes:
            results = await asyncio.gather(
                *[_load_image_on_node(node, t) for node in load_nodes]
            )
            for node, (ok, msg) in zip(load_nodes, results):
                if not ok:
                    t.status = "failed"
                    t.error = f"{node.name} 加载失败: {msg}"
                    db.commit()
                    return
        db.refresh(t)
        if t.status != "loading":
            return

        t.status = "completed"
        db.commit()
    except Exception as e:
        logger.exception("镜像任务监控失败 job=%s", job_id)
        db.rollback()  # 异常可能来自 commit/flush（锁冲突等），先回滚再查询
        t = db.get(ImageTransfer, job_id)
        if t:
            t.status = "failed"
            t.error = str(e)
            db.commit()
    finally:
        db.close()


async def _send_archive_to_node(node: Node, t: ImageTransfer, dest: Path) -> int:
    """管理网发送归档到 head（agent 反向拉取，断点续传）。

    回拉 URL 下发相对路径：Agent 从「下发请求来源 IP」推断控制端地址并补全，
    控制端换机/换 IP 无需任何配置（docker 部署经宿主机 NAT 亦正确）。
    """
    # 认证走 Authorization 头（agent 侧附加共享 token），token 不进 URL
    url = f"/api/images/archive/{t.id}"
    resp = await agent_client.image_pull(
        node, t.image, t.digest or "", url, dest.stat().st_size,
    )
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error") or "Agent 回拉镜像归档失败")
    return dest.stat().st_size


async def _sync_archive_to_worker(
    worker: Node,
    transfer_id: int,
    image: str,
    digest: str,
    size: int,
    source_url: str,
    source_token: str,
) -> tuple[int, dict]:
    """worker 从 head 高速 IP 直拉归档；短期令牌替代 SSH 凭据。"""
    try:
        resp = await agent_client.image_fetch(worker, {
            "source_url": source_url,
            "source_token": source_token,
            "image": image,
            "digest": digest,
            "size": size,
            "transfer_id": transfer_id,
        })
        return worker.id, {
            "status": "completed",
            "transferred_bytes": int(resp.get("bytes") or size),
            "total_bytes": size,
            "source": "high_speed_http",
        }
    except Exception as e:
        return worker.id, {
            "status": "failed", "error": str(e),
            "transferred_bytes": 0, "total_bytes": size,
        }


async def _load_image_on_node(node: Node, t: ImageTransfer) -> tuple[bool, str]:
    """节点执行 docker load 并校验 digest（已有同 digest 镜像跳过）。"""
    try:
        return await agent_client.image_load(node, t.image, t.digest or "")
    except Exception as e:
        return False, str(e)


def resume_image_monitors() -> int:
    """后端重启后恢复进行中的镜像传输监控。"""
    db = SessionLocal()
    count = 0
    try:
        jobs = db.query(ImageTransfer).filter(
            ImageTransfer.status.in_(["pulling", "packing", "sending", "syncing", "loading"])
        ).all()
        for t in jobs:
            if t.status in ("pulling", "packing"):
                threading.Thread(target=_start_pull, args=(t.id, False), daemon=True).start()
            spawn(_monitor_transfer(t.id))
            count += 1
        return count
    finally:
        db.close()
