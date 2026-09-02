"""模型管理编排（控制平面）：

1. downloading : 控制平面经管理网（后端所在机器）用 huggingface_hub 下载到本地 MODEL_CACHE_DIR
2. sending     : head Agent 经管理网逐文件回拉（断点续传 + 内容哈希校验）
3. syncing     : worker Agent 经权威高速地址从 head 并行直拉（无 SSH/rsync）

三个阶段均幂等可续传。模型与任务解耦：发布时可选是否发送模型，终止时可选择是否删除节点模型。
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path

import httpx

from .. import config
from ..background_tasks import spawn
from ..db import SessionLocal
from ..models import ModelDownload, Node, Setting, iso_utc
from . import agent_client, agent_ws, peer_transfer

logger = logging.getLogger(__name__)
POLL_INTERVAL = 5
DEFAULT_ENDPOINT = "https://huggingface.co"
# 下载分片重试次数
CHUNK_RETRIES = 3

# revision（分支名/commit/tag）字符白名单：允许 / 以支持 feature/xxx 等合法的
# 多段分支名，但禁止路径越级（.. / 以 / 开头/结尾）与本地文件系统特殊字符。
_REVISION_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_REVISION_MAX_LEN = 128


def validate_revision(revision: str | None) -> str:
    """校验/规范化模型 revision；非法时抛 ValueError（调用方转为结构化 4xx）。

    覆盖所有下载/分发入口（下载、分发、重试、设置变更重启），确保 revision
    不会作为路径段逃逸出缓存目录（refs/{revision} 写盘、URL 拼接等）。
    """
    if not revision:
        return "main"
    revision = str(revision)
    if len(revision) > _REVISION_MAX_LEN or not _REVISION_RE.match(revision):
        raise ValueError(
            "revision 非法：仅允许字母/数字/._/-（长度 ≤128），以支持分支名如 feature/xx")
    if ".." in revision or revision.startswith("/") or revision.endswith("/") or revision == ".":
        raise ValueError("revision 非法：禁止路径越级（..）或 / 边界")
    return revision


def local_model_dir(repo: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "--", repo)
    # huggingface_hub 的 cache_dir 参数即 hub 根（模型直接落在其下，无 hub/ 子目录）
    return Path(config.MODEL_CACHE_DIR) / f"models--{safe}"


def local_model_size(repo: str) -> int:
    d = local_model_dir(repo)
    if not d.exists():
        return 0
    total = 0
    for f in d.rglob("*"):
        # 跳过 symlink（snapshots/ 链接到 blobs/，避免重复计数）与下载临时文件
        # （含分片 *.part.N：分片名形如 .<hash>.incomplete.part.0，需按 ".part." 判断）
        if f.is_symlink() or not f.is_file():
            continue
        if f.name.endswith((".incomplete", ".lock")) or ".part." in f.name:
            continue
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _download_progress(repo: str) -> int:
    """下载进度（已下载字节数，含进行中的 .incomplete / .part 分片）。

    与 local_model_size（真实内容大小，用于完整性判断）不同：
    进度需要反映"已拉取的字节"，包括尚未完成合并的部分。
    """
    blobs = local_model_dir(repo) / "blobs"
    if not blobs.exists():
        return 0
    total = 0
    for f in blobs.iterdir():
        if f.name.endswith(".lock") or f.is_symlink():
            continue
        if not f.is_file():
            continue
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


# ---------- 下载设置（token / endpoint / 连接数） ----------


def get_hf_settings() -> dict:
    """模型下载配置：endpoint（镜像源）、token、单文件连接数、分片大小、镜像拉取代理。"""
    db = SessionLocal()
    try:
        rows = {k: v for k, v in db.query(Setting.key, Setting.value).all()}
    finally:
        db.close()
    return {
        "endpoint": rows.get("endpoint") or os.environ.get("HF_ENDPOINT") or DEFAULT_ENDPOINT,
        "has_token": bool(rows.get("hf_token")),
        "connections": max(1, min(32, int(rows.get("connections") or 8))),
        "chunk_size_mb": max(1, min(64, int(rows.get("chunk_size_mb") or 8))),
        "docker_proxy": rows.get("docker_proxy") or "",
    }


def set_hf_settings(patch: dict) -> dict:
    """更新模型下载配置。hf_token: 传入 null 清除、字符串则覆盖、缺省保持不变。"""
    db = SessionLocal()
    try:
        for k in ("endpoint", "connections", "chunk_size_mb"):
            v = patch.get(k)
            if v is not None:
                row = db.get(Setting, k)
                if row:
                    row.value = str(v)
                else:
                    db.add(Setting(key=k, value=str(v)))
        # docker_proxy：null 清除、字符串覆盖、缺省保持不变（与 hf_token 一致）
        if "docker_proxy" in patch:
            dp = patch["docker_proxy"]
            if dp is None:
                row = db.get(Setting, "docker_proxy")
                if row:
                    db.delete(row)
            elif dp:
                row = db.get(Setting, "docker_proxy")
                if row:
                    row.value = dp
                else:
                    db.add(Setting(key="docker_proxy", value=dp))
        if "hf_token" in patch:
            tok = patch["hf_token"]
            if tok is None:
                row = db.get(Setting, "hf_token")
                if row:
                    db.delete(row)
            elif tok:
                row = db.get(Setting, "hf_token")
                if row:
                    row.value = tok
                else:
                    db.add(Setting(key="hf_token", value=tok))
        db.commit()
    finally:
        db.close()
    return get_hf_settings()


def _hf_auth(token: str | bool) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _stored_token() -> str | None:
    db = SessionLocal()
    try:
        row = db.get(Setting, "hf_token")
        return row.value if row else None
    finally:
        db.close()


def _manifest_url(repo: str, revision: str, endpoint: str) -> str:
    return f"{endpoint.rstrip('/')}/api/models/{repo}/revision/{revision}?blobs=true"


def _fetch_repo_manifest(repo: str, revision: str, endpoint: str, headers: dict) -> dict:
    """获取仓库文件清单：{sha, siblings: [{rfilename, size, blobId, lfs}]}。

    hf-mirror 等镜像对未缓存仓库会 308 重定向回官方源，需要跟随
    （httpx 跨域重定向会自动丢弃 Authorization）。
    """
    r = httpx.get(_manifest_url(repo, revision, endpoint), headers=headers,
                  timeout=30, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    siblings = []
    for s in data.get("siblings", []):
        sib = {
            "rfilename": s.get("rfilename", ""),
            "size": s.get("size") or 0,
            "blobId": s.get("blobId"),
        }
        # LFS 文件：blob 名与内容校验用 lfs.sha256（内容哈希），blobId 只是指针的 git SHA-1
        lfs = s.get("lfs")
        if lfs:
            sib["lfs"] = {"sha256": lfs.get("sha256"), "size": lfs.get("size")}
        siblings.append(sib)
    return {"sha": data.get("sha", "main"), "siblings": siblings}


def _git_blob_sha1(path: Path) -> str:
    """HF 缓存 blob 名的 git blob SHA-1（'blob <len>\\0<content>'），流式计算避免大文件 OOM。

    git blob 头需要文件总长度，故先 stat 再分块读内容（1MB 分块）。
    """
    size = path.stat().st_size
    h = hashlib.sha1(usedforsecurity=False)
    h.update(b"blob %d\0" % size)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_sha256(path: Path) -> str:
    """流式计算文件 sha256（1MB 分块，避免大模型分片整块读入内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def repo_total_size(repo: str, revision: str = "main") -> int | None:
    """HF 仓库权重总大小（字节），查询失败返回 None。endpoint 使用下载配置。"""
    try:
        s = get_hf_settings()
        token = _stored_token() or os.environ.get("HF_TOKEN") or False
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                _manifest_url(repo, revision, s["endpoint"]),
                headers=_hf_auth(token),
            )
            if r.status_code != 200:
                return None
            data = r.json()
        return sum((sib.get("size") or 0) for sib in data.get("siblings", []))
    except Exception:
        return None


async def resolve_revision_sha(repo: str, revision: str) -> str | None:
    """尽力解析 revision（分支/标签/sha）→ 远端当前 commit sha；失败返回 None。

    只为版本元数据展示/幂等判定服务，解析失败不阻断下载流程
    （下载线程完成后会基于实际清单回填 task.sha）。
    """
    try:
        s = get_hf_settings()
        token = _stored_token() or os.environ.get("HF_TOKEN") or False
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                _manifest_url(repo, validate_revision(revision), s["endpoint"]),
                headers=_hf_auth(token),
            )
            if r.status_code != 200:
                return None
            return (r.json().get("sha") or None)
    except Exception:
        return None


def job_to_dict(job: ModelDownload) -> dict:
    return {
        "id": job.id,
        "repo": job.repo,
        "revision": job.revision,
        "sha": job.sha,
        "head_node_id": job.head_node_id,
        "status": job.status,
        "downloaded_bytes": job.downloaded_bytes,
        "sent_bytes": job.sent_bytes,
        "total_bytes": job.total_bytes,
        "sync_jobs": job.sync_jobs,
        "error": job.error,
        "created_at": iso_utc(job.created_at),
    }


# ---------- 阶段 1：控制平面下载 ----------


def _download_file_chunked(url: str, dest: Path, size: int, connections: int,
                           chunk_mb: int, headers: dict,
                           cancel: threading.Event | None = None) -> None:
    """多连接 Range 分块下载单个文件（参考 bodaay/HuggingFaceModelDownloader 思路）。

    - 分片文件 <dest>.part.<i>；已存在且大小匹配的分片跳过（断点续传）
    - 各分片独立线程并发下载，失败自动重试；cancel 置位时在分片边界优雅退出
    - 全部完成后顺序合并为 <dest>，校验总大小后删除分片
    - 服务器不支持 Range 时降级为整文件单连接下载
    """
    chunk = chunk_mb * 1024 * 1024
    nparts = max(1, math.ceil(size / chunk))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == size:
        return  # 已完整存在
    part_paths = [Path(f"{dest}.part.{i}") for i in range(nparts)]
    ranges = [(i * chunk, min(i * chunk + chunk - 1, size - 1)) for i in range(nparts)]

    # 标记已完成分片（断点续传）
    pending: list[tuple[int, Path]] = []
    for i, (p, (start, end)) in enumerate(zip(part_paths, ranges)):
        if p.exists() and p.stat().st_size == end - start + 1:
            continue
        pending.append((i, p))

    lock = threading.Lock()
    done = 0

    def worker():
        nonlocal done
        client = httpx.Client(headers=headers, timeout=120, follow_redirects=True)
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    return
                try:
                    i, p = pending.pop(0)
                except IndexError:
                    return
                start, end = ranges[i]
                for attempt in range(CHUNK_RETRIES):
                    if cancel is not None and cancel.is_set():
                        return
                    try:
                        with client.stream("GET", url, headers={
                            **headers, "Range": f"bytes={start}-{end}"
                        }) as resp:
                            resp.raise_for_status()
                            with open(p, "wb") as f:
                                for data in resp.iter_bytes(1 << 20):
                                    f.write(data)
                        if p.stat().st_size != end - start + 1:
                            raise RuntimeError(
                                f"分片大小不符 {p.stat().st_size} != {end - start + 1}")
                        with lock:
                            done += 1
                        break
                    except Exception:
                        if attempt == CHUNK_RETRIES - 1:
                            raise
                        time.sleep(1)
        finally:
            client.close()

    workers = min(connections, len(pending)) if pending else 1
    if pending:
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    if cancel is not None and cancel.is_set():
        # 取消：分片保留（下次续传），不合并
        raise _CancelledDownload()

    # 合并分片
    try:
        with open(dest, "wb") as out:
            for p in part_paths:
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, out, 1 << 20)
        if dest.stat().st_size != size:
            raise RuntimeError(f"文件大小不符 {dest.stat().st_size} != {size}")
    finally:
        for p in part_paths:
            p.unlink(missing_ok=True)


class _CancelledDownload(Exception):
    """下载被主动取消（设置变更重启），不视为失败。"""


def _tree_entries(manifest: dict) -> dict:
    """生成 trees 缓存格式：{rfilename: {size, blob_id, lfs_sha256, lfs_size}}。"""
    entries = {}
    for s in manifest.get("siblings", []):
        entry: dict = {"size": s.get("size") or 0, "blob_id": s.get("blobId") or ""}
        lfs = s.get("lfs") or {}
        if lfs.get("sha256"):
            entry["lfs_sha256"] = lfs["sha256"]
            entry["lfs_size"] = lfs.get("size") or s.get("size") or 0
        entries[s["rfilename"]] = entry
    return entries


def _write_hf_layout(repo: str, revision: str, manifest: dict) -> None:
    """写入 HF 标准缓存布局：blobs + snapshots symlinks + refs + trees。"""
    revision = validate_revision(revision)
    d = local_model_dir(repo)
    sha = manifest["sha"]
    refs_dir = d / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    # 纵深防御：即使 revision 通过校验，仍确保 refs 目标解析后位于缓存目录内
    # （防止任何未来调用路径绕过校验把 revision 当作路径段写盘）。
    ref_path = (refs_dir / revision).resolve()
    if not str(ref_path).startswith(str(refs_dir.resolve()) + "/"):
        raise RuntimeError(f"refs 写入路径越界: {revision}")
    (d / "trees").mkdir(parents=True, exist_ok=True)
    snap_dir = d / "snapshots" / sha
    snap_dir.mkdir(parents=True, exist_ok=True)
    (refs_dir / revision).write_text(sha)  # 不带换行：hub 读取 refs 不 strip
    (d / "trees" / f"{sha}.json").write_text(json.dumps(_tree_entries(manifest)))
    for s in manifest["siblings"]:
        rel = s["rfilename"]
        blob = d / "blobs" / s["blobId"]
        link = snap_dir / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(os.path.relpath(blob, link.parent))


def _download_sync(repo: str, revision: str, cancel: threading.Event | None = None) -> str | None:
    """自研分块下载器：清单 API -> 逐文件多连接 Range 分块下载 -> HF 缓存布局。

    token/endpoint/连接数/分片大小均来自 settings（DB 可配置，见 get_hf_settings）；
    每个文件下载前重新读取（设置变更后对新文件即时生效）。
    cancel 置位时在文件/分片边界优雅退出（抛 _CancelledDownload，不视为失败）。
    完成时布局完整（blobs + snapshots symlinks + refs + trees），历史 commit 的快照
    与元数据保留（git 式多版本缓存，切换/回滚零成本）。返回本次解析到的 commit sha。
    """
    def current_settings() -> tuple[str, dict]:
        s = get_hf_settings()
        token = _stored_token() or os.environ.get("HF_TOKEN") or False
        return s["endpoint"], _hf_auth(token)

    revision = validate_revision(revision)  # 全路径防御：下载/分发/重试入口统一校验

    endpoint, headers = current_settings()
    cache = Path(config.MODEL_CACHE_DIR)
    cache.mkdir(parents=True, exist_ok=True)

    # 1) 文件清单（失败即整体失败——无清单无法可靠下载/校验）
    manifest = _fetch_repo_manifest(repo, revision, endpoint, headers)
    d = local_model_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    blobs_dir = d / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)

    # 2) 逐文件多连接分块下载（可续传：已存在同大小 blob 跳过）。
    #    本阶段不动 trees/refs/snapshots：全部文件就位后才写入本次 commit 的布局，
    #    中途失败（网络/校验中断）不会清空已存在的完整缓存——否则已下载模型在
    #    发布分发被重复下载时，元数据先被删、任务却可能已由监控推进到完成，
    #    本地缓存就变成永远无法校验的「残留」。
    for sib in manifest["siblings"]:
        if cancel is not None and cancel.is_set():
            raise _CancelledDownload()
        # 每个文件前重读设置（保存后对新文件即时生效）
        endpoint, headers = current_settings()
        settings = get_hf_settings()
        rel = sib["rfilename"]
        size = sib["size"]
        lfs = sib.get("lfs") or {}
        if size <= 0:
            # 空文件（如仓库里的 __init__.py）：内容确定（0 字节），无需网络下载；
            # 但必须落一个内容寻址的 0 字节 blob——否则 snapshots symlink 悬空，
            # _verify_snapshot 永远判缺该文件，重试/「补全」无法自愈。
            expected = lfs.get("sha256") if lfs else sib.get("blobId")
            tmp = blobs_dir / f".{expected or 'empty'}.incomplete"
            tmp.write_bytes(b"")
            got = _file_sha256(tmp) if lfs else _git_blob_sha1(tmp)
            if expected and got != expected:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"内容校验失败: {rel} ({got[:12]} != {expected[:12]})")
            blob_id = got
            blob = blobs_dir / blob_id
            if blob.is_file() and blob.stat().st_size == 0:
                tmp.unlink(missing_ok=True)
            else:
                tmp.rename(blob)
            sib["blobId"] = blob_id
            continue
        # blob 命名/内容校验规则：
        # - LFS 文件（清单带 lfs 字段）：blob 名 = 内容 sha256（lfs.sha256）
        # - 普通文件：blob 名 = git blob SHA-1（blobId，'blob <len>\0<content>'）
        if lfs:
            blob_id = lfs.get("sha256")
            expected = blob_id  # 内容 sha256
        else:
            blob_id = sib.get("blobId")
            expected = blob_id  # 用 git blob SHA-1 校验（与 blob 文件名同源）
        # 目标 blob 已存在且大小匹配 -> 复用（文件名即内容哈希，天然去重）
        if blob_id:
            target = blobs_dir / blob_id
            if target.is_file() and target.stat().st_size == size:
                # 关键：LFS 文件的 blobId 是 lfs.sha256（内容哈希），
                # 跳过下载时必须回写到清单，否则布局写入会指向 git 指针名
                sib["blobId"] = blob_id
                continue
        else:
            existing = [b for b in blobs_dir.iterdir()
                        if b.is_file()
                        and not (b.name.endswith((".incomplete", ".lock")) or ".part." in b.name)
                        and b.stat().st_size == size]
            if existing:
                sib["blobId"] = existing[0].name
                continue
        url = f"{endpoint.rstrip('/')}/{repo}/resolve/{manifest['sha']}/{rel}"
        dest = blobs_dir / f".{blob_id or hashlib.sha1(rel.encode()).hexdigest()[:8]}.incomplete"
        _download_file_chunked(url, dest, size, settings["connections"],
                               settings["chunk_size_mb"], headers, cancel)
        # 内容校验（拦截损坏/被篡改内容），并确定 blob 文件名（流式哈希，避免大文件 OOM）
        if lfs:
            got = _file_sha256(dest)
            if expected and got != expected:
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"内容校验失败: {rel} (sha256 {got[:12]} != {expected[:12]})")
        else:
            got = _git_blob_sha1(dest)
            if expected and got != expected:
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"内容校验失败: {rel} (git sha1 {got[:12]} != {expected[:12]})")
        sib["blobId"] = blob_id or got
        dest.rename(blobs_dir / sib["blobId"])

    # 3) 全部文件就位后写入本次 commit 的引用布局（snapshots/<sha> + trees/<sha>.json
    #    + refs/<revision> → sha）。HF 缓存即 git 式多版本存储：已有历史 commit 的
    #    snapshots/trees/refs 一律保留，本仓可并存任意多个版本，切换/回滚零成本。
    _write_hf_layout(repo, revision, manifest)

    # 4) 按本次 commit 逐文件完整性校验（缺任一文件即失败，绝不进入发送阶段）
    v = _verify_snapshot(repo, manifest["sha"])
    if not v["ok"]:
        raise RuntimeError(v["error"])
    return manifest["sha"]


def _verify_snapshot(repo: str, sha: str) -> dict:
    """校验指定 commit 快照：trees/<sha>.json 元数据 vs snapshots/<sha> symlink 目标。

    trees 为新版 hub 格式 {rfilename: {size, blob_id, ...}}，commit 取文件名（sha）。
    返回 {"ok": bool, "total": int, "missing": [...], "error": str|None}
    """
    d = local_model_dir(repo)
    t = d / "trees" / f"{sha}.json"
    if not t.is_file():
        return {"ok": False, "total": 0, "missing": [],
                "error": f"缺少 {sha[:12]} 的 trees 元数据（该版本未下载/元数据残缺）"}
    try:
        data = json.loads(t.read_text())
    except Exception:
        return {"ok": False, "total": 0, "missing": [],
                "error": f"{sha[:12]} 的 trees 清单损坏"}
    entries = {k: v for k, v in data.items() if isinstance(v, dict) and "size" in v}
    if not entries:
        return {"ok": False, "total": 0, "missing": [],
                "error": f"{sha[:12]} 的 trees 清单无效/为空"}
    snap = d / "snapshots" / sha
    missing: list[str] = []
    total = 0
    for rel, info in entries.items():
        size = info.get("size") or 0
        total += size
        link = snap / rel
        if not (link.is_symlink() and link.exists()):
            missing.append(rel)
            continue
        blob = link.resolve()
        if not blob.is_file() or blob.stat().st_size != size:
            missing.append(rel)
    if missing:
        return {
            "ok": False, "total": total, "missing": missing[:20],
            "error": f"完整性校验失败：{len(missing)} 个文件缺失/不完整"
                     f"（{', '.join(missing[:5])}…）",
        }
    return {"ok": True, "total": total, "missing": [], "error": None}


def _active_snapshot(repo: str) -> tuple[str | None, str | None]:
    """当前激活版本 (revision 名, sha)。

    refs/* 是修订指针，指向「本仓当前使用的版本」：以它为权威，即使目标
    commit 不完整/损坏也如实返回，避免 fallback 到历史完整快照而掩盖当前
    版本缺失。完全没有 refs（旧缓存）时退化为最新写入的完整快照。
    """
    d = local_model_dir(repo)
    refs: dict[str, str] = {}
    for p in (d / "refs").glob("*"):
        try:
            refs[p.name] = p.read_text().strip()
        except Exception:
            continue
    for name in sorted(refs, key=lambda n: (n != "main", n)):
        if refs[name]:
            return name, refs[name]
    trees = sorted((d / "trees").glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for t in trees:
        if _verify_snapshot(repo, t.stem)["ok"]:
            return None, t.stem
    return None, None


def _snapshot_versions(repo: str) -> list[dict]:
    """从缓存目录派生已知 commit 版本列表（最新在前，用于 UI 展示）。

    每项：{sha, total_size（该版本清单逻辑大小）, files, complete}。
    """
    d = local_model_dir(repo)
    out: list[dict] = []
    trees = sorted((d / "trees").glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for t in trees:
        sha = t.stem
        try:
            data = json.loads(t.read_text())
        except Exception:
            continue
        entries = {k: v for k, v in data.items() if isinstance(v, dict) and "size" in v}
        if not entries:
            continue
        out.append({
            "sha": sha,
            "total_size": sum(int(v.get("size") or 0) for v in entries.values()),
            "files": len(entries),
            "complete": _verify_snapshot(repo, sha)["ok"],
        })
    return out


def _ref_sha(repo: str, revision: str) -> str | None:
    """refs/<revision> 指向的 commit sha；无该引用返回 None。"""
    ref = local_model_dir(repo) / "refs" / validate_revision(revision)
    if not ref.is_file():
        return None
    val = ref.read_text().strip()
    return val or None


def prune_repo_versions(repo: str, keep: int = 3) -> list[str]:
    """清理历史版本（GC）：删除不被引用、且不属于最新 keep 个完整版本的快照。

    保护：任一 refs 指向的版本、当前激活版本、以及进行中任务的目标版本；
    再额外保留最新 keep 个完整版本。blobs 按内容寻址天然去重、不删除
    （可能被保留版本引用）。删除对象 = snapshots/<sha> 目录 + trees/<sha>.json +
    指向已删版本的 refs 条目。返回被清理的 sha 列表。
    """
    d = local_model_dir(repo)
    if not d.exists():
        return []
    protected: set[str] = set()
    refs_dir = d / "refs"
    if refs_dir.is_dir():
        for ref in refs_dir.glob("*"):
            try:
                protected.add(ref.read_text().strip())
            except Exception:
                continue
    db = SessionLocal()
    try:
        active = db.query(ModelDownload).filter(
            ModelDownload.repo == repo,
            ModelDownload.status.in_(["downloading", "sending", "syncing", "paused"]),
        ).all()
        for job in active:
            if job.sha:
                protected.add(job.sha)
    finally:
        db.close()
    versions = _snapshot_versions(repo)
    # 额外保留最新 keep 个完整版本（防止频繁「更新到最新」后历史版本被一次清光）
    newest_complete = [v["sha"] for v in versions if v["complete"]][:keep]
    protected |= set(newest_complete)
    deleted: list[str] = []
    for v in versions:
        if v["sha"] in protected:
            continue
        snap = d / "snapshots" / v["sha"]
        tree = d / "trees" / f"{v['sha']}.json"
        if snap.exists():
            shutil.rmtree(snap)
        if tree.exists():
            tree.unlink()
        deleted.append(v["sha"])
    if deleted and refs_dir.is_dir():
        for ref in refs_dir.glob("*"):
            try:
                if ref.read_text().strip() in deleted:
                    ref.unlink()
            except Exception:
                continue
    return deleted


def _verify_local_model(repo: str) -> dict:
    """校验控制平面缓存（激活版本）：trees 元数据 vs snapshots symlink 目标 blobs 大小。

    多版本共存时以「激活版本」为准（见 _active_snapshot），避免历史完整快照
    掩盖当前激活版本缺失/损坏。
    """
    _, sha = _active_snapshot(repo)
    if sha is None:
        return {"ok": False, "total": 0, "missing": [],
                "error": "模型未下载（无完整快照）"}
    return _verify_snapshot(repo, sha)


# ---------- 跨种类并发下载互斥（模型 <-> 镜像） ----------
#
# 控制平面同一时间只允许一个「外部下载源」（模型 HF 拉取 / 镜像 registry 拉取），
# 避免两个下载任务同时抢管理平面带宽与磁盘 IO。已完整缓存的资源后续只做分发
# （sending/syncing），不与对方下载冲突，可与对方并发进行。


def _model_job_target_ready(job) -> bool:
    """该模型传输任务的控制平面目标版本是否已完整（不再需要真实下载）。"""
    if job.sha:
        return _verify_snapshot(job.repo, job.sha)["ok"]
    return _local_cache_ready(job.repo, job.revision)


def _reject_if_image_pulling() -> None:
    """有镜像正在拉取时拒绝开始模型下载（调用方把 ValueError 转为 409）。

    「正在拉取」按归档是否落盘判定：有进行中拉取任务（pulling/packing）且
    归档文件尚未生成 = 真实外部下载；已缓存的镜像只分发，不构成并发下载。
    用本模块 SessionLocal 查询（与调用方同一数据库会话源），归档路径借用
    image_manager 的纯函数。
    """
    from ..models import ImageTransfer
    from .image_manager import image_archive_path

    db = SessionLocal()
    try:
        pulls = db.query(ImageTransfer).filter(
            ImageTransfer.status.in_(["pulling", "packing"]),
        ).all()
    finally:
        db.close()
    for t in pulls:
        dest = image_archive_path(t.image, t.digest)
        if dest.exists() and dest.stat().st_size > 0:
            continue  # 归档已就绪：分发前快速跳过，不构成并发下载
        raise ValueError(
            f"镜像 {t.image} 正在下载（任务 #{t.id}），不能与模型同时下载；"
            "请等待其完成或取消后再下载模型")


# 进行中的下载线程注册表（job_id -> 线程 / 取消事件），供设置变更时优雅重启
_download_threads: dict[int, threading.Thread] = {}
_download_cancel: dict[int, threading.Event] = {}

_ACTIVE_STATUSES = ("downloading", "sending", "syncing")
# 暂停时记录原阶段，继续时回到该阶段（下载阶段重启线程续传；发送/同步阶段幂等重跑）
_paused_phase: dict[int, str] = {}


def _start_local_download(job_id: int, repo: str, revision: str) -> None:
    """启动下载线程（注册到全局表，支持取消/重启）。取消不标记失败（任务保持 downloading）。"""

    def run():
        cancel = _download_cancel.get(job_id)
        try:
            sha = _download_sync(repo, revision, cancel)
            if sha:
                # 回填解析出的 commit sha（版本元数据：任务记录了实际下载的版本）
                db = SessionLocal()
                try:
                    job = db.get(ModelDownload, job_id)
                    if job:
                        job.sha = sha
                        db.commit()
                finally:
                    db.close()
        except _CancelledDownload:
            pass  # 主动取消（设置变更重启/暂停/取消），任务状态由调用方管理
        except Exception as e:
            db = SessionLocal()
            try:
                job = db.get(ModelDownload, job_id)
                if job and job.status == "downloading":
                    job.status = "failed"
                    job.error = f"下载失败: {e}"
                    db.commit()
            finally:
                db.close()
        finally:
            # 仅当注册表里还是本线程/本事件时才清理（避免覆盖重启后的新线程条目）
            if _download_threads.get(job_id) is threading.current_thread():
                _download_threads.pop(job_id, None)
            if _download_cancel.get(job_id) is cancel:
                _download_cancel.pop(job_id, None)

    ev = _download_cancel.setdefault(job_id, threading.Event())
    ev.clear()
    t = threading.Thread(target=run, daemon=True)
    _download_threads[job_id] = t
    t.start()


async def pause_download(job_id: int) -> dict:
    """暂停下载/分发任务：置取消事件，下载线程在分片边界自行退出（不阻塞等待），
    已下载分片保留（可续传）。"""
    db = SessionLocal()
    try:
        job = db.get(ModelDownload, job_id)
        if not job:
            raise ValueError("下载任务不存在")
        if job.status not in _ACTIVE_STATUSES:
            raise ValueError(f"任务当前状态 {job.status}，无法暂停")
        _paused_phase[job_id] = job.status
        ev = _download_cancel.get(job_id)
        if ev:
            ev.set()  # 线程在下一个分片边界退出（在途分片下载完即停）
        job.status = "paused"
        job.error = None
        db.commit()
        return job_to_dict(job)
    finally:
        db.close()


async def resume_download(job_id: int) -> dict:
    """继续暂停的任务：等待旧线程完全退出后，下载阶段重启线程（.part 分片续传）；
    发送/同步阶段幂等重跑。"""
    db = SessionLocal()
    try:
        job = db.get(ModelDownload, job_id)
        if not job:
            raise ValueError("下载任务不存在")
        if job.status != "paused":
            raise ValueError(f"任务当前状态 {job.status}，无法继续")
        phase = _paused_phase.pop(job_id, "downloading")
        # 继续到下载阶段且目标版本缺失会重启真实下载线程：遵守跨种类并发下载互斥，
        # 在写回 downloading 状态前检查，拒绝时不留下「无线程的下载中」任务。
        if phase == "downloading" and not _local_cache_ready(job.repo, job.revision):
            _reject_if_image_pulling()
        job.status = phase
        job.error = None
        db.commit()
        if phase == "downloading":
            t = _download_threads.get(job_id)
            if t and t.is_alive():
                # 旧线程已在退出（取消事件已置位）：等待其完全结束再开新线程，
                # 避免两个线程写同一批 .part 分片。join 是阻塞调用，放线程池。
                await asyncio.to_thread(t.join, 120)
            t = _download_threads.get(job_id)
            if not t or not t.is_alive():
                _start_local_download(job.id, job.repo, job.revision)
        spawn(_monitor_job(job.id))
        return job_to_dict(job)
    finally:
        db.close()


async def retry_download_job(job_id: int) -> ModelDownload:
    """就地重试失败任务（复用原 job_id，不新建任务记录）。

    失败任务统一回到 downloading 阶段重启：
    - 控制平面下载失败：重启下载线程，已下载分片/blobs 断点续传；
    - 发送/同步阶段失败（分发）：本地缓存完整前提下下载线程快速跳过，
      监控幂等推进 sending -> syncing（head/worker 已落地的文件续传跳过）。

    幂等续传/阶段推进由 _monitor_job 保证，因此重试不再新建记录——
    UI 上始终只有同一条任务，避免「旧失败 + 新下载」两条并存被误读为再次失败。
    """
    db = SessionLocal()
    try:
        job = db.get(ModelDownload, job_id)
        if not job:
            raise ValueError("下载任务不存在")
        if job.status != "failed":
            raise ValueError(f"任务当前状态 {job.status}，无法重试")
        # 若该 repo 已有别的进行中任务，就地重试会与其并发写同一缓存/重复下发
        other_active = db.query(ModelDownload).filter(
            ModelDownload.repo == job.repo,
            ModelDownload.id != job.id,
            ModelDownload.status.in_(["downloading", "sending", "syncing", "paused"]),
        ).first()
        if other_active:
            raise ValueError(f"该模型已有进行中的传输任务 #{other_active.id}，请等待完成或删除后重试")
        _paused_phase.pop(job.id, None)
        # 目标版本缺失的重试会重新开启真实下载：先做跨种类互斥检查，
        # 避免已把状态置回 downloading 后才发现被拒而留下「无线程的下载中」任务。
        if not _local_cache_ready(job.repo, job.revision):
            _reject_if_image_pulling()
        # 防御：失败仍可能由监控侧标记（下载线程还活着），先协作取消并等其退出，
        # 避免新旧下载线程并发写同一批 .part 分片。
        t = _download_threads.get(job.id)
        if t and t.is_alive():
            ev = _download_cancel.get(job.id)
            if ev:
                ev.set()
            await asyncio.to_thread(t.join, 120)
        job.status = "downloading"
        job.error = None
        db.commit()
        t = _download_threads.get(job.id)
        if not t or not t.is_alive():
            # 目标 revision 缓存已完整（如发送/同步阶段失败）时不重跑破坏性下载，
            # 直接由监控校验通过后继续发送/同步；否则重启下载线程补齐缓存。
            if not _local_cache_ready(job.repo, job.revision):
                _start_local_download(job.id, job.repo, job.revision)
        spawn(_monitor_job(job.id))
        return job
    finally:
        db.close()


async def cancel_download(job_id: int) -> dict:
    """取消下载/分发任务：停止下载线程，标记 cancelled（分片保留，可重试续传）。"""
    db = SessionLocal()
    try:
        job = db.get(ModelDownload, job_id)
        if not job:
            raise ValueError("下载任务不存在")
        if job.status not in _ACTIVE_STATUSES + ("paused",):
            raise ValueError(f"任务当前状态 {job.status}，无法取消")
        _paused_phase.pop(job_id, None)
        ev = _download_cancel.get(job_id)
        if ev:
            ev.set()
        job.status = "cancelled"
        job.error = "用户取消"
        db.commit()
        return job_to_dict(job)
    finally:
        db.close()


def restart_downloads_with_new_settings() -> int:
    """下载设置变更后，重启所有进行中的下载任务以应用新设置。

    协作式取消：置 cancel 标志 -> 等待旧线程在分片边界退出（最多 120s）-> 启动新线程
    （分片/blobs 保留，断点续传）。返回重启的任务数。
    """
    db = SessionLocal()
    try:
        jobs = db.query(ModelDownload).filter(
            ModelDownload.status == "downloading"
        ).all()
        targets = [(j.id, j.repo, j.revision) for j in jobs]
    finally:
        db.close()
    for job_id, repo, revision in targets:
        ev = _download_cancel.get(job_id)
        if ev:
            ev.set()
        t = _download_threads.get(job_id)
        if t and t.is_alive():
            t.join(timeout=120)
        if not _download_threads.get(job_id):  # 旧线程已退出
            _start_local_download(job_id, repo, revision)
        else:
            # join 超时（旧线程卡在慢速分片读取且不检查 cancel）：绝不能放任
            # 任务停留在 downloading 且无活线程，否则该 repo 的重新下载/分发
            # 会被进行中守卫永久拒绝。安排守护：旧线程真正退出并清理注册后
            # 立即用新设置重启下载（分片保留，续传安全）。
            _schedule_restart_watchdog(job_id, repo, revision)
    return len(targets)


def _schedule_restart_watchdog(job_id: int, repo: str, revision: str) -> None:
    """join 超时后的兜底：等待旧下载线程彻底退出，随后用新设置重启同任务。

    旧线程退出（finally）会弹出注册项；守护仅当「注册项仍指向这个旧线程」时
    才重启，避免与用户手动重试/resume 双重拉起新线程。
    """

    t = _download_threads.get(job_id)
    if not t:
        _start_local_download(job_id, repo, revision)
        return

    def _watch():
        try:
            t.join()  # cancel 已置位，旧线程应在分片边界退出；极慢读取也终会结束
        except Exception:
            logger.exception("下载重启看门狗等待异常 job=%s", job_id)
            return
        try:
            if _download_threads.get(job_id) is not t:
                return  # 已被其它路径重启/取消，不重复拉起
            _download_threads.pop(job_id, None)
            _start_local_download(job_id, repo, revision)
        except Exception:
            logger.exception("下载重启看门狗异常 job=%s", job_id)

    threading.Thread(target=_watch, daemon=True, name=f"fw-restart-watch-{job_id}").start()


# ---------- 阶段 2：管理网发送到 head（agent 反向拉取，GET 流式可靠） ----------
#
# 回拉 URL 下发「相对路径」（不带 IP/端口）：Agent 从「下发请求的来源 IP」推断
# 控制端地址并补全 http://<来源IP>:8000 —— 控制端换机/换 IP 无需任何配置
# （docker 部署经宿主机 NAT，节点看到的来源 IP 恰为宿主机管理网 IP）。


def _model_file_integrity(path: Path, rel: Path) -> tuple[str, str]:
    """返回 Agent 端校验所需摘要；HF blob 直接复用内容寻址文件名。"""
    if rel.parts and rel.parts[0] == "blobs" and re.fullmatch(r"[0-9a-f]{64}", path.name):
        return "sha256", path.name
    if rel.parts and rel.parts[0] == "blobs" and re.fullmatch(r"[0-9a-f]{40}", path.name):
        return "git-sha1", path.name
    return "sha256", _file_sha256(path)


def _snapshot_send_manifest(repo: str, sha: str) -> list[dict]:
    """目标 commit 快照的待发送文件清单（按 sha 精确分发，不再整仓全量下发）。

    只包含该版本需要的最小集合：snapshots/<sha> 条目（symlink）+ 引用 blobs +
    trees/<sha>.json + 指向该 sha 的 refs 锚点。节点据此获得完整且唯一的该版本布局，
    历史版本不再被顺带全量分发。
    """
    d = local_model_dir(repo)
    snap = d / "snapshots" / sha
    trees_path = d / "trees" / f"{sha}.json"
    if not trees_path.is_file():
        raise ValueError(f"目标版本 {sha[:12]} 元数据缺失（trees），无法发送")
    files: dict[str, dict] = {}
    blob_refs: set[str] = set()

    def add_file(path: Path, rel: Path) -> None:
        key = rel.as_posix()
        if key in files:
            return
        if path.is_symlink():
            files[key] = {"rel": key, "symlink": os.readlink(path),
                          "size": 0, "hash_algo": None, "digest": None}
            return
        if not path.is_file():
            return
        algo, digest = _model_file_integrity(path, rel)
        files[key] = {"rel": key, "symlink": None, "size": path.stat().st_size,
                      "hash_algo": algo, "digest": digest}

    # 快照条目（symlink 指向 blobs/<内容哈希>）
    if snap.is_dir():
        for p in sorted(snap.rglob("*")):
            if p.is_symlink():
                blob_refs.add(Path(os.readlink(p)).name)
            add_file(p, p.relative_to(d))
    # 引用的 blob 文件（内容寻址，天然去重）
    blobs_dir = d / "blobs"
    for name in sorted(blob_refs):
        add_file(blobs_dir / name, Path(f"blobs/{name}"))
    # 布局元数据：trees/<sha>.json + 所有指向该 sha 的 refs（main/分支/sha 锚点）
    add_file(trees_path, Path(f"trees/{sha}.json"))
    refs_dir = d / "refs"
    if refs_dir.is_dir():
        for ref in sorted(refs_dir.glob("*")):
            try:
                if ref.read_text().strip() == sha:
                    add_file(ref, Path(f"refs/{ref.name}"))
            except Exception:
                continue
    if not files:
        raise ValueError(f"目标版本 {sha[:12]} 无可用文件")
    return list(files.values())


async def _send_repo_to_node(node: Node, repo: str, on_progress,
                             transfer_id: int,
                             should_continue=None,
                             sha: str | None = None) -> int:
    """逐文件让节点从控制平面拉取（保持 HF hub 布局：blobs + snapshots symlink + refs）。

    - sha 给定：只发送该 commit 的最小文件集合（版本精确分发，历史版本不下发）；
    - 文件级并发 4（管理网带宽充裕时显著提速；agent 侧线程池可并行处理）；
    - 单文件失败重试 1 次（agent 侧 .part 断点续传，重试成本低）；
    - should_continue：每文件前回调，返回 False 时停止（pause/取消协作退出）。
    """
    if sha:
        files = _snapshot_send_manifest(repo, sha)
    else:
        # 无显式版本（理论兜底）：整仓发送，与旧行为一致
        src = local_model_dir(repo)
        files = []
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            parts = rel.parts
            if any(p in (".locks", ".no_exist") for p in parts):
                continue
            if path.name.endswith((".incomplete", ".lock")) or ".part." in path.name:
                continue
            if path.is_symlink() or path.is_file():
                add = {"rel": rel.as_posix()}
                if path.is_symlink():
                    add["symlink"] = os.readlink(path)
                    add["size"] = 0
                    add["hash_algo"] = add["digest"] = None
                else:
                    algo, digest = _model_file_integrity(path, rel)
                    add.update(size=path.stat().st_size, hash_algo=algo, digest=digest)
                files.append(add)

    sent = 0
    sem = asyncio.Semaphore(4)
    sent_lock = asyncio.Lock()

    async def pull_one(entry: dict):
        nonlocal sent
        rel = entry["rel"]
        file_url = f"/api/models/files/{repo}?relpath={rel}"
        async with sem:
            if should_continue is not None and not await should_continue():
                return  # 任务被暂停/取消：剩余文件不再发送
            if entry.get("symlink"):
                await agent_client.model_pull(
                    node, repo, rel, file_url, 0, transfer_id=transfer_id,
                    symlink=entry["symlink"],
                )
                return
            size = entry["size"]
            for attempt in range(2):  # 重试 1 次：agent .part 断点续传
                try:
                    await agent_client.model_pull(
                        node, repo, rel, file_url, size,
                        hash_algo=entry["hash_algo"], digest=entry["digest"],
                        transfer_id=transfer_id,
                    )
                    break
                except Exception:
                    if attempt == 1:
                        raise
                    await asyncio.sleep(1)
            async with sent_lock:
                sent += size
                await on_progress(sent)

    await asyncio.gather(*(pull_one(f) for f in files))
    return sent


# ---------- 阶段 3：head -> worker（Agent 高速 HTTP 直传） ----------


def _update_sync_job(transfer_id: int, node_id: int, patch: dict) -> None:
    """独立会话更新单 worker 进度，避免并发协程共享 SQLAlchemy Session。"""
    db = SessionLocal()
    try:
        job = db.get(ModelDownload, transfer_id)
        if not job:
            return
        jobs = dict(job.sync_jobs or {})
        info = dict(jobs.get(str(node_id)) or {})
        info.update(patch)
        jobs[str(node_id)] = info
        job.sync_jobs = jobs
        db.commit()
    finally:
        db.close()


def _transfer_is_syncing(transfer_id: int) -> bool:
    db = SessionLocal()
    try:
        job = db.get(ModelDownload, transfer_id)
        return bool(job and job.status == "syncing")
    finally:
        db.close()


def _load_trees(repo: str, sha: str) -> dict:
    """读取 trees/<sha>.json：{rfilename: {size, blob_id, ...}}（缺失/损坏返回空）。"""
    t = local_model_dir(repo) / "trees" / f"{sha}.json"
    try:
        data = json.loads(t.read_text())
    except Exception:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and "size" in v}


def _worker_delta_manifest(share_manifest: list, repo: str, sha: str,
                           missing_rels: list | None, truncated: bool = False):
    """把 worker 缺失的逻辑文件映射为 head share manifest 子集（集群内差量直传）。

    缺失文件 = snapshots/<sha>/<rel> symlink + 其引用的 blobs/<id>，外加
    trees/<sha>.json 与 refs 锚点（节点补齐布局）。missing_rels 为 None
    （旧 Agent / 无法按版本判定）或 truncated（缺失超出 Agent 清单上限、
    按该子集补齐会造成快照永远不完整）表示用全量 manifest。
    返回 (delta_manifest, delta_total)。
    """
    if missing_rels is None or truncated:
        return None, 0
    trees = _load_trees(repo, sha)
    wanted = {f"snapshots/{sha}/{r}".rstrip("/") for r in missing_rels}
    for r in missing_rels:
        bid = (trees.get(r) or {}).get("blob_id")
        if bid:
            wanted.add(f"blobs/{bid}")
    wanted.add(f"trees/{sha}.json")
    delta = [
        e for e in share_manifest
        if (e.get("relpath") or "") in wanted
        or (e.get("relpath") or "").startswith("refs/")
    ]
    return delta, sum(int(e.get("size") or 0) for e in delta)


async def _node_model_state(node: Node, repo: str, sha: str) -> dict | None:
    """查询节点目标 commit 缓存状态；旧 Agent（不支持按版本校验）返回 None。"""
    try:
        st = await agent_client.model_cache_repo(node, repo, sha=sha)
    except Exception:
        return None
    if st.get("verify_sha") != sha:
        return None
    return st


async def _sync_model_to_worker(
    worker: Node,
    transfer_id: int,
    repo: str,
    manifest: list[dict],
    total_size: int,
    source_url: str,
    source_token: str,
    existing_job_id: str | None = None,
) -> tuple[int, dict]:
    """启动 worker 后台直拉并轮询；父任务暂停/取消时立即停止 Agent 子任务。"""
    fetch_job_id = existing_job_id
    done = 0

    async def wait_until_stopped() -> dict:
        """等待 Agent 确认停止，避免恢复任务与旧任务并发写同一 .part。"""
        deadline = time.monotonic() + 120
        while True:
            status = await agent_client.model_fetch_status(worker, fetch_job_id)
            if status.get("status") in ("completed", "failed", "cancelled"):
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待 {worker.name} 停止模型直传超时")
            await asyncio.sleep(POLL_INTERVAL)

    try:
        if fetch_job_id:
            try:
                existing = await agent_client.model_fetch_status(worker, fetch_job_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                fetch_job_id = None
            else:
                if existing.get("status") == "cancelling":
                    existing = await wait_until_stopped()
                if existing.get("status") == "completed":
                    return worker.id, {
                        "job_id": fetch_job_id, "status": "completed",
                        "transferred_bytes": total_size, "total_bytes": total_size,
                        "source": "high_speed_http",
                    }
                if existing.get("status") in ("failed", "cancelled"):
                    fetch_job_id = None
        if not fetch_job_id:
            resp = await agent_client.model_fetch(worker, {
                "source_url": source_url,
                "source_token": source_token,
                "repo": repo,
                "manifest": manifest,
                "total_size": total_size,
                "transfer_id": transfer_id,
                "connections": 4,
            })
            fetch_job_id = resp["job_id"]
        _update_sync_job(transfer_id, worker.id, {
            "job_id": fetch_job_id, "status": "syncing",
            "transferred_bytes": 0, "total_bytes": total_size,
            "source": "high_speed_http",
        })
        while True:
            if not _transfer_is_syncing(transfer_id):
                await agent_client.model_fetch_cancel(worker, fetch_job_id)
                stopped = await wait_until_stopped()
                done = int(stopped.get("transferred_bytes") or done)
                if stopped.get("status") == "completed":
                    return worker.id, {
                        "job_id": fetch_job_id, "status": "completed",
                        "transferred_bytes": total_size, "total_bytes": total_size,
                        "source": "high_speed_http",
                    }
                return worker.id, {
                    "job_id": fetch_job_id, "status": "paused",
                    "transferred_bytes": done, "total_bytes": total_size,
                    "source": "high_speed_http",
                }
            status = await agent_client.model_fetch_status(worker, fetch_job_id)
            done = int(status.get("transferred_bytes") or 0)
            current = status.get("current_file")
            _update_sync_job(transfer_id, worker.id, {
                "status": "syncing", "transferred_bytes": done,
                "total_bytes": total_size, "current_file": current,
            })
            if status.get("status") == "completed":
                return worker.id, {
                    "job_id": fetch_job_id, "status": "completed",
                    "transferred_bytes": total_size, "total_bytes": total_size,
                    "source": "high_speed_http",
                }
            if status.get("status") in ("failed", "cancelled"):
                return worker.id, {
                    "job_id": fetch_job_id, "status": status["status"],
                    "error": status.get("error") or "模型直传失败",
                    "transferred_bytes": done, "total_bytes": total_size,
                    "source": "high_speed_http",
                }
            await asyncio.sleep(POLL_INTERVAL)
    except Exception as e:
        return worker.id, {
            "job_id": fetch_job_id, "status": "failed",
            "error": f"{worker.name} 无法从 head 高速地址拉取模型 ({source_url}): {e}",
            "transferred_bytes": done, "total_bytes": total_size,
            "source": "high_speed_http",
        }


# ---------- 总编排 ----------


def _local_cache_ready(repo: str, revision: str) -> bool:
    """控制平面是否已有目标 revision 的完整可用缓存。

    refs/<revision> 把 revision 解析成 commit sha，再按该 sha 的完整快照判定，
    不会再被其它历史完整快照误判为就绪。满足时无需跑 _download_sync（对已完整
    版本是浪费），可直接复用它分发/发布；只满足其一（如缓存的其它 revision）
    则仍需下载该版本。
    """
    sha = _ref_sha(repo, revision)
    if not sha:
        return False
    return _verify_snapshot(repo, sha)["ok"]


def _ensure_ref_anchor(repo: str, sha: str) -> None:
    """确保 refs/<sha> 锚点存在（指定版本分发/发布时节点可据此固定该 commit）。"""
    if not sha:
        return
    ref = local_model_dir(repo) / "refs" / sha
    if not ref.is_file() or ref.read_text().strip() != sha:
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(sha)


async def start_download_job(repo: str, revision: str, head_node_id: int | None,
                             sync_node_ids: list[int], initial_status: str = "downloading",
                             sha: str | None = None, force: bool = False) -> ModelDownload:
    """创建模型传输任务：控制平面下载 ->（可选）发送 head -> Agent 高速直传 worker。

    - head_node_id 为 None：仅下载到控制平面缓存，不向任何节点分发
    - initial_status="sending"（分发任务）：跳过下载阶段，直接从本地缓存发送/同步
      （调用方需保证本地模型已完整，见 _verify_local_model）
    - sha 给定：以该 commit 为目标（版本切换/按版本分发）——本地完整则直接复用，
      不完整则按该 sha 续传/补齐；分发（sending）模式必须本地已完整
    - force=True：即使目标 revision 缓存已完整也重新解析远端并增量补齐
      （「更新到最新」：上游 main 漂移时把 refs 推进到新 commit）
    """
    db = SessionLocal()
    try:
        if head_node_id is not None:
            head = db.get(Node, head_node_id)
            if not head:
                raise ValueError("head 节点不存在")
        # 同 repo 已有进行中任务则拒绝（防止并发发送互相覆盖）
        active = db.query(ModelDownload).filter(
            ModelDownload.repo == repo,
            ModelDownload.status.in_(["downloading", "sending", "syncing", "paused"]),
        ).first()
        if active:
            raise ValueError(f"该模型已有进行中的传输任务 #{active.id}（{active.status}），请等待完成或删除后重试")
        # 目标版本解析：显式 sha 优先，其次 revision（refs 缓存 → 远端尽力解析）
        if sha is not None:
            if _verify_snapshot(repo, sha)["ok"]:
                _ensure_ref_anchor(repo, sha)
                target_sha, fetch_revision, need_fetch = sha, None, False
            elif initial_status == "sending":
                raise ValueError(
                    f"目标版本 {sha[:12]} 未完整下载，无法分发；请先切换到该版本再分发")
            else:
                target_sha, fetch_revision, need_fetch = sha, sha, True
        else:
            target_sha = _ref_sha(repo, revision)
            if target_sha is None:
                target_sha = await resolve_revision_sha(repo, revision)
            if force and initial_status == "downloading":
                # 强制刷新（更新到最新）：以远端当前 commit 作为任务目标，而不是
                # 缓存里的旧 sha——否则 job.sha 停在旧版本，监控会因旧版本已完整
                # 提前放行/分发，新版本还在后台下载时任务就按旧版本完成了。
                # 远端未漂移（或解析失败）时按本地缓存完整性决定是否补齐（修复）。
                remote_sha = await resolve_revision_sha(repo, revision)
                if remote_sha and remote_sha != target_sha:
                    target_sha = remote_sha
                    need_fetch = True
                else:
                    need_fetch = not _local_cache_ready(repo, revision)
            else:
                need_fetch = not _local_cache_ready(repo, revision)
            fetch_revision = revision
        # 跨种类互斥：本次需要真实下载（目标版本未就绪）时，禁止与进行中的
        # 镜像拉取并发；已缓存的版本只分发不下载，不受此限制。
        if initial_status == "downloading" and need_fetch:
            _reject_if_image_pulling()
        total = await repo_total_size(repo, fetch_revision or revision)
        job = ModelDownload(
            repo=repo,
            revision=revision,
            sha=target_sha,
            head_node_id=head_node_id,
            status=initial_status,
            total_bytes=total,
            sync_jobs={str(nid): {"status": "pending"} for nid in sync_node_ids},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        if initial_status == "downloading" and need_fetch:
            # 目标版本本地不完整/需要强制刷新：启动下载线程补齐（增量续传，
            # 已有 blobs 跳过；全文件就位后才写布局，中途失败不破坏现有缓存）。
            _start_local_download(job.id, repo, fetch_revision or revision)
        spawn(_monitor_job(job.id))
        return job
    finally:
        db.close()


async def _monitor_job(job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(ModelDownload, job_id)
        if not job:
            return
        head = db.get(Node, job.head_node_id) if job.head_node_id else None
        if job.head_node_id and not head:
            job.status = "failed"
            job.error = "head 节点不存在"
            db.commit()
            return

        # 阶段 1：等待控制平面本地下载完成。
        # 完成判定 = 任务目标 commit（job.sha）的完整快照就绪；sha 缺失时退回
        # refs/<revision> 就绪判定。按目标 sha 判定让「强制更新到最新」也不会在
        # 新版本下载完成前基于旧版本提前放行。
        while job.status == "downloading":
            db.refresh(job)
            job.downloaded_bytes = _download_progress(job.repo)
            db.commit()
            if job.sha:
                if _verify_snapshot(job.repo, job.sha)["ok"]:
                    break
            elif _local_cache_ready(job.repo, job.revision):
                break
            await asyncio.sleep(POLL_INTERVAL)

        if job.status in ("failed", "cancelled", "paused"):
            return  # 失败/用户取消/暂停：不再推进流程

        if head is None:
            # 仅下载到控制平面：完整性校验已通过，任务即完成（不分发节点）
            job.status = "completed"
            job.downloaded_bytes = _download_progress(job.repo)
            db.commit()
            return

        # 分发目标版本：优先任务记录的 sha，缺失时取激活版本
        target_sha = job.sha
        if not target_sha:
            _, target_sha = _active_snapshot(job.repo)
        if not target_sha:
            job.status = "failed"
            job.error = "无法确定目标版本（sha 未解析且无可用快照）"
            db.commit()
            return

        target_nodes = [head]
        for node_id in (job.sync_jobs or {}):
            worker = db.get(Node, int(node_id))
            if worker:
                target_nodes.append(worker)
        capability_errors = []
        for node in target_nodes:
            error = await peer_transfer.check_agent_capability(
                node, agent_client, "model_peer_transfer_v1",
            )
            if error:
                capability_errors.append(error)
        if capability_errors:
            job.status = "failed"
            job.error = "Agent 能力检查失败：" + "；".join(capability_errors)
            db.commit()
            return

        # 阶段 2：管理网发送到 head（幂等续传；文件级并发 + 协作暂停）。
        # 后端若在 syncing 阶段重启，head 已完整，直接接管 worker 子任务。
        if job.status != "syncing":
            job.status = "sending"
            job.sent_bytes = 0
            db.commit()
            try:
                async def on_progress(sent: int):
                    job.sent_bytes = sent
                    db.commit()

                async def should_continue() -> bool:
                    db.refresh(job)
                    return job.status == "sending"

                await _send_repo_to_node(
                    head, job.repo, on_progress, transfer_id=job.id,
                    should_continue=should_continue, sha=target_sha,
                )
            except Exception as e:
                job.status = "failed"
                job.error = f"发送到 head 失败: {e}"
                db.commit()
                return
            finally:
                agent_ws.clear_model_file_progress(job.id)
            db.refresh(job)
            if job.status != "sending":
                return  # 发送期间被暂停/取消

        # 阶段 3：worker Agent 经权威高速地址从 head Agent 并行直拉。
        job.status = "syncing"
        db.commit()
        if not job.sync_jobs:
            job.status = "completed"
            job.downloaded_bytes = local_model_size(job.repo)
            db.commit()
            return
        try:
            share = await agent_client.model_share(head, job.repo)
            head_ip = peer_transfer.node_transfer_ip(db, head)
            # 校验 head 返回的路径/令牌，防止注入 userinfo 把 worker 拉取重定向到外部主机
            share_path = peer_transfer.validate_share_path(share.get("path"))
            share_token = peer_transfer.validate_share_token(share.get("token"))
            source_url = f"http://{head_ip}:{head.agent_port}{share_path}"
            manifest = share.get("manifest") or []
            total_size = int(share.get("total_size") or 0)
            if not manifest or total_size <= 0:
                raise RuntimeError("head 返回的模型 manifest 为空")
        except Exception as e:
            job.status = "failed"
            job.error = f"head 开放高速模型传输失败: {e}"
            db.commit()
            return

        workers: list[Node] = []
        worker_manifests: dict[int, tuple[list, int]] = {}
        initial_jobs = dict(job.sync_jobs or {})
        existing_job_ids: dict[int, str] = {}
        for nid_str in initial_jobs:
            worker = db.get(Node, int(nid_str))
            if not worker:
                initial_jobs[nid_str] = {"status": "failed", "error": "worker 不存在"}
                continue
            previous = dict(initial_jobs.get(nid_str) or {})
            if previous.get("status") == "completed":
                continue
            # 新 Agent：按目标 commit 精确判定——已具备则跳过，缺失则只补差量
            st = await _node_model_state(worker, job.repo, target_sha)
            if st and st.get("complete"):
                initial_jobs[nid_str] = {
                    **previous, "status": "completed",
                    "transferred_bytes": 0, "total_bytes": 0,
                    "source": "cached",
                }
                continue
            workers.append(worker)
            if st:
                dm, dtotal = _worker_delta_manifest(
                    manifest, job.repo, target_sha, st.get("missing"),
                    truncated=bool(st.get("truncated")))
                if dm is not None and dm:
                    worker_manifests[worker.id] = (dm, dtotal)
            if previous.get("job_id") and previous.get("status") in (
                "syncing", "running", "cancelling",
            ):
                existing_job_ids[worker.id] = previous["job_id"]
            worker_total = total_size
            if worker.id in worker_manifests:
                worker_total = worker_manifests[worker.id][1]
            initial_jobs[nid_str] = {
                **previous,
                "status": "syncing",
                "transferred_bytes": int(previous.get("transferred_bytes") or 0),
                "total_bytes": worker_total, "source": "high_speed_http",
            }
        job.sync_jobs = initial_jobs
        db.commit()

        async def sync_one(worker: Node):
            m, t = worker_manifests.get(worker.id) or (manifest, total_size)
            return await _sync_model_to_worker(
                worker, job.id, job.repo, m, t,
                source_url, share_token, existing_job_ids.get(worker.id),
            )

        results = await asyncio.gather(*[sync_one(w) for w in workers])
        db.refresh(job)
        merged_jobs = dict(job.sync_jobs or {})
        for node_id, result in results:
            merged_jobs[str(node_id)] = result
        job.sync_jobs = merged_jobs
        db.commit()
        if job.status != "syncing":
            return

        all_ok = all(j.get("status") == "completed" for j in merged_jobs.values())
        job.status = "completed" if all_ok else "failed"
        if not all_ok:
            failed = [
                f"#{nid} {j.get('error') or '未知错误'}"[:300]
                for nid, j in (job.sync_jobs or {}).items()
                if j.get("status") != "completed"
            ]
            job.error = "部分 worker 同步失败" + (f"：{'；'.join(failed)}" if failed else "")
        job.downloaded_bytes = local_model_size(job.repo)
        db.commit()
    except Exception as e:
        logger.exception("模型任务监控失败 job=%s", job_id)
        db.rollback()  # 异常可能来自 commit/flush（锁冲突等），先回滚再查询
        job = db.get(ModelDownload, job_id)
        if job:
            job.status = "failed"
            job.error = str(e)
            db.commit()
    finally:
        db.close()


async def ensure_model_on_nodes(repo: str, revision: str, nodes: list[Node],
                                head_node_id: int | None = None,
                                sha: str | None = None) -> dict:
    """任务发布前保障模型就绪（控制平面缓存 + head + 各 worker 按目标版本完整）。

    返回 {"ok": bool, "missing": [...], "download_job_id": int|None, "message": str}。
    节点核查按「目标 commit sha」精确进行：新 Agent 校验该版本完整；旧 Agent
    （不支持按版本）退化到 size 阈值语义。sha 给定（发布固定版本）时启用版本钉扎。
    """
    total = await repo_total_size(repo, revision)
    threshold = total * 0.99 if total else 0
    target_sha = sha or _ref_sha(repo, revision)
    if target_sha is None:
        target_sha = await resolve_revision_sha(repo, revision)
    db = SessionLocal()
    try:
        # 1) 各节点是否已有目标版本完整缓存（决定能否直接发布）
        node_missing = []
        for n in nodes:
            if target_sha:
                st = await _node_model_state(n, repo, target_sha)
            else:
                st = None
            if st is not None:
                if not st.get("complete"):
                    node_missing.append({
                        "where": n.name, "cached": False,
                        "need": target_sha[:7] if target_sha else None,
                        "missing": st.get("missing"),
                        "truncated": bool(st.get("truncated")),
                    })
                continue
            # 旧 Agent / 目标 sha 未解析：按 size 阈值回退（原有语义）
            try:
                st_old = await agent_client.model_cache_repo(n, repo)
            except Exception:
                node_missing.append({"where": n.name, "cached": False, "error": "agent 不可达"})
                continue
            cached = bool(st_old.get("cached"))
            if cached and total:
                cached = (st_old.get("size_bytes") or 0) >= threshold
            if not cached:
                node_missing.append({"where": n.name, "cached": False})
        if not node_missing:
            return {"ok": True, "missing": [], "download_job_id": None,
                    "message": "模型已完整就绪（全部节点已覆盖目标版本"
                               + (f" {target_sha[:7]}" if target_sha else "") + "）"}

        # 2) 有节点缺失 -> 控制平面完整源 -> 管理网发送 head -> Agent 高速直传
        missing = []
        if target_sha:
            # 版本钉扎：控制平面按目标 commit 精确校验（而不只是激活版本），
            # 避免 main 漂移后钉扎旧版本却误报控制平面已就绪。
            if not _verify_snapshot(repo, target_sha)["ok"]:
                missing.append({"where": "控制平面", "cached": False})
        elif not _local_cache_ready(repo, revision):
            missing.append({"where": "控制平面", "cached": False})
        missing += node_missing

        head_id = head_node_id or (nodes[0].id if nodes else None)
        if head_id is None:
            raise ValueError("无可用节点")
        job = await start_download_job(repo, revision, head_id,
                                       [n.id for n in nodes if n.id != head_id],
                                       sha=sha if sha else None)
        return {
            "ok": False,
            "missing": missing,
            "download_job_id": job.id,
            "message": f"模型未完整就绪（{', '.join(m['where'] for m in missing)}），已启动传输任务 #{job.id}（目标版本 {job.sha[:7] if job.sha else revision}）",
        }
    finally:
        db.close()


def resume_download_monitors() -> int:
    """后端重启后恢复进行中的模型传输监控。

    下载线程随进程重启丢失：对仍不完整的 downloading 任务重新启动下载线程
    （分片/blobs 断点续传），其余任务仅恢复监控轮询。
    """
    db = SessionLocal()
    count = 0
    try:
        jobs = db.query(ModelDownload).filter(
            ModelDownload.status.in_(["downloading", "sending", "syncing"])
        ).all()
        for job in jobs:
            # 下载线程随进程重启丢失：目标版本（job.sha，缺失时退回 refs/revision）
            # 不完整才重启下载线程续传（分片/blobs 断点续传），其余仅恢复监控轮询。
            # 按 job.sha 判定而不是激活版本：强制刷新等任务 job.sha 会领先于 refs，
            # 用激活版本判定会把这类任务误判为就绪而不再续传。
            if job.status == "downloading":
                ready = (_verify_snapshot(job.repo, job.sha)["ok"] if job.sha
                         else _local_cache_ready(job.repo, job.revision))
                if not ready:
                    _start_local_download(job.id, job.repo, job.sha or job.revision)
            spawn(_monitor_job(job.id))
            count += 1
        return count
    finally:
        db.close()
