"""镜像分发编排（方案 A：管理平面下载分发，与模型分发同构）。

1. pulling : 控制平面用 skopeo 从公网 registry 拉取为 docker-archive（tar）
2. sending : 管理网发送 head（agent 反向拉取，GET 流式，断点续传）
3. syncing : head 经 RoCE 高速计算网（SSH/rsync）同步到各 worker
4. loading : 各节点 docker load（已有同 digest 镜像自动跳过）+ digest 校验

各阶段幂等可续传。解决多节点同时向公网拉镜像的带宽竞争/网络不稳定问题。
"""

import asyncio
import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import threading

import httpx

from sqlalchemy.orm.attributes import flag_modified
from pathlib import Path

from ..db import SessionLocal
from ..background_tasks import spawn
from ..models import ImageTransfer, Node, iso_utc
from . import agent_client

logger = logging.getLogger(__name__)
POLL_INTERVAL = 5

IMAGE_CACHE_DIR = Path(os.environ.get("IMAGE_CACHE_DIR", "./images-cache"))


def image_archive_path(image: str, digest: str | None = None) -> Path:
    """控制平面镜像归档文件路径（docker-archive tar）。

    文件名固定用镜像名哈希（digest 拉取前可能未知，避免路径漂移）。
    """
    safe = hashlib.sha256(image.encode()).hexdigest()[:24]
    return IMAGE_CACHE_DIR / f"{safe}.tar"


def _proxy_env() -> dict | None:
    """拉取代理环境变量（settings.docker_proxy -> HTTP_PROXY/HTTPS_PROXY）。

    返回 None 表示未配置（不注入）；skopeo 直接做 HTTP 请求，尊重这些变量。
    """
    from .model_manager import get_hf_settings

    proxy = get_hf_settings().get("docker_proxy") or ""
    proxy = proxy.strip()
    if not proxy:
        return None
    env = dict(os.environ)
    env["HTTP_PROXY"] = proxy
    env["HTTPS_PROXY"] = proxy
    env.setdefault("NO_PROXY", "127.0.0.1,localhost")
    return env


def _run(cmd: list[str], timeout: int = 3600, env: dict | None = None) -> str:
    """执行外部命令（skopeo），失败抛 RuntimeError。"""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} 失败: {proc.stderr.strip()[:500]}")
    return proc.stdout


# ---------- Python registry 客户端（skopeo 不可用时的兜底，代理完全可控） ----------


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
    except Exception:  # noqa: BLE001
        return ""


def _registry_token(client: httpx.Client, host: str, path: str) -> str:
    """registry 匿名 token（401 -> Bearer token 流程），公开仓库无需认证时返回空。"""
    try:
        r = client.get(f"https://{host}/v2/", follow_redirects=True)
    except Exception:  # noqa: BLE001
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
                        token: str, dest: Path, expect_size: int | None = None) -> None:
    """流式下载 registry blob 到文件：Range 断点续传 + 连接中断重试 + sha256 校验。

    大镜像层（可达 GB 级）经代理传输易中断（peer closed / timeout）：
    - 流式落盘 .part，避免整块加载内存；
    - 中断后用 Range: bytes=N- 从断点续传（服务器不支持 Range 时返回 200，从头重下）；
    - 连接类错误最多重试 5 次，token 失效（401）时重新获取；
    - 完成后 sha256 校验（续传时纳入已下载字节）；
    - Range 越界（416）时 .part 已完整，先校验内容再收尾落盘，避免重复撞 416 卡死。
    """
    if dest.exists():
        return
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
                    token2 = _token_from_challenge(client, r.headers.get("www-authenticate", ""))
                    if not token2:
                        r.raise_for_status()
                    token = token2
                    continue  # 换 token 后从头重试（清空已下载）
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
                            return
                    tmp.unlink(missing_ok=True)
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
                if expect_size and tmp.stat().st_size != expect_size:
                    raise RuntimeError(
                        f"blob 大小不符: {tmp.stat().st_size} != {expect_size}")
                got = "sha256:" + h.hexdigest()
                if digest.startswith("sha256:") and got != digest:
                    raise RuntimeError(
                        f"blob sha256 校验失败: {got[:16]} != {digest[:16]}")
                tmp.rename(dest)
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

    registry 的 layer blob 是 gzip 压缩 tar，需解压为 plain tar（docker load 格式）。
    大层从文件流式解压/写入，避免整块加载内存。
    """
    import tarfile

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
        # layers（gzip 解压为 plain tar，流式写入）
        for (ld, p), name in zip(layer_files, layer_names):
            plain = p.with_suffix(".plain")
            try:
                with open(p, "rb") as f:
                    is_gzip = f.read(2) == b"\x1f\x8b"
                if is_gzip:
                    with gzip.open(p, "rb") as gz, open(plain, "wb") as pf:
                        shutil.copyfileobj(gz, pf, 1 << 20)
                    layer_path = plain
                else:
                    layer_path = p
                ti3 = tarfile.TarInfo(name)
                ti3.size = layer_path.stat().st_size
                with open(layer_path, "rb") as lf:
                    out.addfile(ti3, lf)
            finally:
                if is_gzip:
                    plain.unlink(missing_ok=True)


def _pull_via_registry(image: str, dest: Path, proxy: str | None) -> None:
    """Python registry API 拉取镜像（强制 linux/arm64，支持代理）。

    大层流式落盘 + Range 断点续传 + 连接中断重试（代理传输不稳定时的容错）。
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
        layer_files: list[tuple[str, Path]] = []
        blob_dir = IMAGE_CACHE_DIR / ".blobs"
        blob_dir.mkdir(parents=True, exist_ok=True)
        try:
            for i, ld in enumerate((m.get("digest") for m in manifest.get("layers", []))):
                if not ld:
                    continue
                lp = blob_dir / ld.replace("sha256:", "")[:24]
                if not lp.exists():
                    _registry_blob_file(client, host, path, ld, token, lp)
                layer_files.append((ld, lp))
            _build_docker_archive(image, manifest, cfg_blob, layer_files, dest)
        finally:
            # 下载中断时的临时分片随 .part 残留，下次续传复用；不清理 blob 缓存
            pass
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
        except Exception:  # noqa: BLE001
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


def _proxy_is_socks(proxy: str | None) -> bool:
    """socks 代理：skopeo（Go net/http）不支持，必须走 Python registry 路径。"""
    return bool(proxy and proxy.startswith(("socks5://", "socks4://", "socks://")))


def inspect_image(image: str) -> dict:
    """查询镜像元数据（digest/大小/架构），强制 linux/arm64 视图。

    优先 skopeo（--override-arch arm64），不可用或 socks 代理时走 Python registry API。
    """
    proxy = _proxy_value()
    env = _proxy_env()
    if shutil.which("skopeo") and not _proxy_is_socks(proxy):
        try:
            out = _run(
                ["skopeo", "inspect", "--override-arch", "arm64", "--override-os", "linux",
                 f"docker://{image}"],
                timeout=120, env=env,
            )
            data = json.loads(out)
            arch = data.get("Architecture", "")
            os_name = data.get("Os", "")
            if arch and os_name and (os_name != "linux" or arch not in ("arm64", "aarch64")):
                raise ValueError(
                    f"镜像平台 {os_name}/{arch} 不适用于 DGX Spark（需要 linux/arm64）")
            layers_data = data.get("LayersData") or []
            size = sum(l.get("Size") or 0 for l in layers_data)
            digest = data.get("Digest", "")
            return {
                "image": image,
                "digest": digest if digest.startswith("sha256:") else f"sha256:{digest}",
                "size_bytes": size,
                "layers": len(data.get("Layers") or []),
                "arch": arch or "arm64",
                "os": os_name or "linux",
            }
        except Exception:  # noqa: BLE001
            # skopeo 失败（网络/权限）回退 Python 路径
            pass
    return _inspect_via_registry(image, proxy)


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


def pull_image(image: str, dest: Path) -> None:
    """拉取镜像为 docker-archive（tar），强制 linux/arm64，支持 http/https/socks5 代理。

    优先 skopeo copy（socks 代理除外，Go 不支持）；否则 Python registry API。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        if shutil.which("skopeo") and not _proxy_is_socks(_proxy_value()):
            try:
                _run(
                    ["skopeo", "copy", "--override-arch", "arm64", "--override-os", "linux",
                     f"docker://{image}", f"docker-archive:{tmp}"],
                    timeout=7200, env=_proxy_env(),
                )
                tmp.rename(dest)
                return
            except Exception:  # noqa: BLE001
                tmp.unlink(missing_ok=True)
        _pull_via_registry(image, tmp, _proxy_value())
        tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)


def image_transfer_to_dict(t: ImageTransfer) -> dict:
    return {
        "id": t.id,
        "image": t.image,
        "digest": t.digest,
        "head_node_id": t.head_node_id,
        "status": t.status,
        "downloaded_bytes": t.downloaded_bytes,
        "sent_bytes": t.sent_bytes,
        "size_bytes": t.size_bytes,
        "sync_jobs": t.sync_jobs,
        "error": t.error,
        "created_at": iso_utc(t.created_at),
    }


# ---------- 阶段状态机 ----------

_ACTIVE_STATUSES = ("pulling", "sending", "syncing", "loading")
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
        phase = _paused_phase.pop(job_id, "sending")
        t.status = phase
        t.error = None
        db.commit()
        if phase == "pulling":
            dest = image_archive_path(t.image, t.digest)
            pt = _pull_threads.get(job_id)
            if not (dest.exists() and dest.stat().st_size > 0) and (not pt or not pt.is_alive()):
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
            ImageTransfer.status.in_(["pulling", "sending", "syncing", "loading", "paused"]),
        ).first()
        if active:
            raise ValueError(f"该镜像已有进行中的传输任务 #{active.id}（{active.status}）")
        info = inspect_image(image)
        t = ImageTransfer(
            image=image,
            digest=info["digest"],
            head_node_id=head_node_id,
            status="pulling",
            size_bytes=info["size_bytes"],
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
        except Exception:  # noqa: BLE001
            missing.append(f"{n.name}（agent 不可达）")
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
    """阶段 1：控制平面 skopeo/Python registry 拉取镜像为 docker-archive（线程）。

    force=True 时忽略已有归档强制重新拉取（刷新最新版本）。
    注册到 _pull_threads 供暂停/继续查询；拉取为子进程无法中途终止，
    暂停/取消只标记状态，归档完成后由监控流程停在后续阶段。
    """
    _t = threading.current_thread()
    _pull_threads[job_id] = _t
    db = SessionLocal()
    try:
        t = db.get(ImageTransfer, job_id)
        if not t:
            return
        dest = image_archive_path(t.image, t.digest)
        if force and dest.exists():
            dest.unlink(missing_ok=True)
        if not (dest.exists() and dest.stat().st_size > 0):
            pull_image(t.image, dest)
        # 统一 digest：归档文件 sha256 指纹（构建确定性，跨节点字节一致）
        t.digest = _archive_fingerprint(dest)
        t.downloaded_bytes = dest.stat().st_size if dest.exists() else 0
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("镜像拉取失败 job=%s: %s", job_id, e)
        db.rollback()
        t = db.get(ImageTransfer, job_id)
        if t and t.status in ("pulling", "paused"):
            t.status = "failed"
            t.error = f"拉取失败: {e}"
            db.commit()
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

        # 阶段 1：等待控制平面拉取完成（归档文件就绪 + digest 统一为归档指纹）
        while t.status == "pulling":
            db.refresh(t)
            dest = image_archive_path(t.image, t.digest)
            if dest.exists() and dest.stat().st_size > 0:
                # 统一 digest：归档文件 sha256 指纹（幂等，同文件重算一致）。
                # 必须在进入 sending 前完成，避免发送/同步/加载各阶段 digest 不一致
                t.digest = _archive_fingerprint(dest)
                db.commit()
                break
            await asyncio.sleep(POLL_INTERVAL)
        if t.status in ("failed", "cancelled", "paused"):
            return  # 失败/用户取消/暂停：不再推进流程

        if head is None:
            # 仅下载到控制平面
            t.status = "completed"
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
        except Exception as e:  # noqa: BLE001
            t.status = "failed"
            t.error = f"发送到 head 失败: {e}"
            db.commit()
            return
        db.refresh(t)
        if t.status != "sending":
            return  # 发送期间被暂停/取消

        # 阶段 3：head 经 RoCE 同步到各 worker
        t.status = "syncing"
        db.commit()
        all_ok = True
        for nid_str in list((t.sync_jobs or {}).keys()):
            db.refresh(t)
            if t.status != "syncing":
                return  # 同步期间被暂停/取消
            worker = db.get(Node, int(nid_str))
            if not worker:
                t.sync_jobs[nid_str].update(status="failed", error="worker 不存在")
                all_ok = False
                continue
            if not await _sync_archive_to_worker(head, worker, t):
                all_ok = False
            t.sync_jobs = dict(t.sync_jobs)
            flag_modified(t, "sync_jobs")
            db.commit()
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

        # 阶段 4：head + 各 worker 节点 docker load（已有同 digest 跳过）
        t.status = "loading"
        db.commit()
        for nid_str in ["head"] + list((t.sync_jobs or {}).keys()):
            db.refresh(t)
            if t.status != "loading":
                return  # 加载期间被暂停/取消
            node = head if nid_str == "head" else db.get(Node, int(nid_str))
            if not node:
                continue
            ok, msg = await _load_image_on_node(node, t)
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
    except Exception as e:  # noqa: BLE001
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
    await agent_client.image_pull(node, t.image, t.digest or "", url)
    return dest.stat().st_size


async def _sync_archive_to_worker(head: Node, worker: Node, t: ImageTransfer) -> bool:
    """head -> worker RoCE rsync 归档文件。"""
    from .model_manager import _roce_ip

    roce_ip = _roce_ip(worker) or worker.ip
    try:
        resp = await agent_client.image_sync(head, {
            "target_host": roce_ip,
            "target_user": worker.ssh_username or "spark",
            "target_port": worker.ssh_port,
            "image": t.image,
            "digest": t.digest or "",
        })
        info = t.sync_jobs[str(worker.id)]
        info.update(job_id=resp["job_id"], status="syncing")
        while True:
            s = await agent_client.image_sync_status(head, resp["job_id"])
            if s.get("status") == "completed":
                info.update(status="completed")
                return True
            if s.get("status") == "failed":
                info.update(status="failed", error=s.get("error"))
                return False
            await asyncio.sleep(POLL_INTERVAL)
    except Exception as e:  # noqa: BLE001
        t.sync_jobs[str(worker.id)].update(status="failed", error=str(e))
        return False


async def _load_image_on_node(node: Node, t: ImageTransfer) -> tuple[bool, str]:
    """节点执行 docker load 并校验 digest（已有同 digest 镜像跳过）。"""
    try:
        return await agent_client.image_load(node, t.image, t.digest or "")
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def resume_image_monitors() -> int:
    """后端重启后恢复进行中的镜像传输监控。"""
    db = SessionLocal()
    count = 0
    try:
        jobs = db.query(ImageTransfer).filter(
            ImageTransfer.status.in_(["pulling", "sending", "syncing", "loading"])
        ).all()
        for t in jobs:
            if t.status == "pulling":
                dest = image_archive_path(t.image, t.digest)
                if not (dest.exists() and dest.stat().st_size > 0):
                    threading.Thread(target=_start_pull, args=(t.id, False), daemon=True).start()
            spawn(_monitor_transfer(t.id))
            count += 1
        return count
    finally:
        db.close()
