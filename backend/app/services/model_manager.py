"""模型管理编排（控制平面）：

1. downloading : 控制平面经管理网（后端所在机器）用 huggingface_hub 下载到本地 MODEL_CACHE_DIR
2. sending     : 逐文件流式上传到 head 节点（管理网，断点续传——已存在同大小文件自动跳过）
3. syncing     : head 经 RoCE 高速计算网（SSH/rsync）同步到各 worker

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

from sqlalchemy.orm.attributes import flag_modified
from pathlib import Path

import httpx

from .. import config
from ..background_tasks import spawn
from ..db import SessionLocal
from ..models import ModelDownload, Node, Setting, iso_utc
from . import agent_client

logger = logging.getLogger(__name__)
POLL_INTERVAL = 5
DEFAULT_ENDPOINT = "https://huggingface.co"
# 下载分片重试次数
CHUNK_RETRIES = 3


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
        if f.is_symlink() or not f.is_file():
            continue
        if f.name.endswith((".incomplete", ".lock")):
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


async def repo_total_size(repo: str) -> int | None:
    """HF 仓库权重总大小（字节），查询失败返回 None。endpoint 使用下载配置。"""
    try:
        s = get_hf_settings()
        token = _stored_token() or os.environ.get("HF_TOKEN") or False
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                _manifest_url(repo, "main", s["endpoint"]),
                headers=_hf_auth(token),
            )
            if r.status_code != 200:
                return None
            data = r.json()
        return sum((sib.get("size") or 0) for sib in data.get("siblings", []))
    except Exception:  # noqa: BLE001
        return None


def _roce_ip(node: Node) -> str | None:
    hw = node.hardware_info or {}
    for r in hw.get("roce") or []:
        if r.get("rocev2_ip"):
            return r["rocev2_ip"]
    return None


def job_to_dict(job: ModelDownload) -> dict:
    return {
        "id": job.id,
        "repo": job.repo,
        "revision": job.revision,
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
                    except Exception:  # noqa: BLE001
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
    """hub 1.23+ 兼容的 trees 缓存格式：{rfilename: {size, blob_id, lfs_sha256, lfs_size}}。

    新版 huggingface_hub 用 trees 元数据校验快照完整性，旧版 {"sha", "siblings"} 格式
    会被误读为缺文件（把 sha/siblings 当文件名）导致 LocalEntryNotFoundError。
    """
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
    d = local_model_dir(repo)
    sha = manifest["sha"]
    (d / "refs").mkdir(parents=True, exist_ok=True)
    (d / "trees").mkdir(parents=True, exist_ok=True)
    snap_dir = d / "snapshots" / sha
    snap_dir.mkdir(parents=True, exist_ok=True)
    (d / "refs" / revision).write_text(sha)  # 不带换行：hub 读取 refs 不 strip
    (d / "trees" / f"{sha}.json").write_text(json.dumps(_tree_entries(manifest)))
    for s in manifest["siblings"]:
        rel = s["rfilename"]
        blob = d / "blobs" / s["blobId"]
        link = snap_dir / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(os.path.relpath(blob, link.parent))


def _download_sync(repo: str, revision: str, cancel: threading.Event | None = None) -> None:
    """自研分块下载器：清单 API -> 逐文件多连接 Range 分块下载 -> HF 缓存布局。

    token/endpoint/连接数/分片大小均来自 settings（DB 可配置，见 get_hf_settings）；
    每个文件下载前重新读取（设置变更后对新文件即时生效）。
    cancel 置位时在文件/分片边界优雅退出（抛 _CancelledDownload，不视为失败）。
    完成时布局完整（blobs + snapshots symlinks + refs + trees），
    供 _verify_local_model 逐文件校验与后续发送/同步使用。
    """
    def current_settings() -> tuple[str, dict]:
        s = get_hf_settings()
        token = _stored_token() or os.environ.get("HF_TOKEN") or False
        return s["endpoint"], _hf_auth(token)

    endpoint, headers = current_settings()
    cache = Path(config.MODEL_CACHE_DIR)
    cache.mkdir(parents=True, exist_ok=True)

    # 1) 文件清单（失败即整体失败——无清单无法可靠下载/校验）
    manifest = _fetch_repo_manifest(repo, revision, endpoint, headers)
    d = local_model_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    blobs_dir = d / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    # 清理旧元数据（旧版 snapshot_download 的 trees 格式不兼容，会干扰完整性校验；
    # blobs 保留用于断点续传）
    for sub in ("trees", "refs", "snapshots"):
        p = d / sub
        if p.exists():
            shutil.rmtree(p)
    # 清理旧版下载器（snapshot_download）残留：无点前缀的 .incomplete / .part 文件。
    # 当前下载器文件均以点前缀开头（.XXX.incomplete / .part.N），不受影响。
    for f in blobs_dir.glob("[!.]*.part.*"):
        f.unlink(missing_ok=True)
    for f in blobs_dir.glob("[!.]*.incomplete"):
        f.unlink(missing_ok=True)

    # 2) 逐文件多连接分块下载（可续传：已存在同大小 blob 跳过）
    for sib in manifest["siblings"]:
        if cancel is not None and cancel.is_set():
            raise _CancelledDownload()
        # 每个文件前重读设置（保存后对新文件即时生效）
        endpoint, headers = current_settings()
        settings = get_hf_settings()
        rel = sib["rfilename"]
        size = sib["size"]
        if size <= 0:
            continue
        # blob 命名/内容校验规则：
        # - LFS 文件（清单带 lfs 字段）：blob 名 = 内容 sha256（lfs.sha256）
        # - 普通文件：blob 名 = git blob SHA-1（blobId，'blob <len>\0<content>'）
        lfs = sib.get("lfs") or {}
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
                        and not b.name.endswith((".incomplete", ".part", ".lock"))
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

    # 3) 写入 HF 标准布局（snapshots symlinks / refs / trees）
    _write_hf_layout(repo, revision, manifest)

    # 4) 逐文件完整性校验（缺任一文件即失败，绝不进入发送阶段）
    v = _verify_local_model(repo)
    if not v["ok"]:
        raise RuntimeError(v["error"])


def _verify_local_model(repo: str) -> dict:
    """逐文件校验控制平面缓存：trees 元数据 vs snapshots symlink 目标 blobs 大小。

    trees 为新版 hub 格式 {rfilename: {size, blob_id, ...}}，commit 取文件名。
    返回 {"ok": bool, "total": int, "missing": [...], "error": str|None}
    """
    d = local_model_dir(repo)
    trees = list((d / "trees").glob("*.json"))
    if not trees:
        return {"ok": False, "total": 0, "missing": [], "error": "缺少 trees 元数据（模型未下载）"}
    # 选第一个格式有效（非空且含 size 字段条目）的清单；旧版/残缺清单一律视为未下载
    data = None
    sha = None
    for t in trees:
        try:
            cand = json.loads(t.read_text())
        except Exception:  # noqa: BLE001
            continue
        entries = cand if isinstance(cand, dict) else {}
        if any(isinstance(v, dict) and "size" in v for v in entries.values()):
            data = entries
            sha = t.stem
            break
    if data is None:
        return {"ok": False, "total": 0, "missing": [], "error": "trees 清单无效/为空（模型未完整下载）"}
    snap = d / "snapshots" / sha
    missing: list[str] = []
    total = 0
    for rel, info in data.items():
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
            "error": f"完整性校验失败：{len(missing)} 个文件缺失/不完整（{', '.join(missing[:5])}…）",
        }
    return {"ok": True, "total": total, "missing": [], "error": None}


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
            _download_sync(repo, revision, cancel)
        except _CancelledDownload:
            pass  # 主动取消（设置变更重启/暂停/取消），任务状态由调用方管理
        except Exception as e:  # noqa: BLE001
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
    return len(targets)


# ---------- 阶段 2：管理网发送到 head（agent 反向拉取，GET 流式可靠） ----------
#
# 回拉 URL 下发「相对路径」（不带 IP/端口）：Agent 从「下发请求的来源 IP」推断
# 控制端地址并补全 http://<来源IP>:8000 —— 控制端换机/换 IP 无需任何配置
# （docker 部署经宿主机 NAT，节点看到的来源 IP 恰为宿主机管理网 IP）。


async def _send_repo_to_node(node: Node, repo: str, on_progress,
                             should_continue=None) -> int:
    """逐文件让节点从控制平面拉取（保持 HF hub 布局：blobs + snapshots symlink + refs）。

    - 文件级并发 4（管理网带宽充裕时显著提速；agent 侧线程池可并行处理）；
    - 单文件失败重试 1 次（agent 侧 .part 断点续传，重试成本低）；
    - should_continue：每文件前回调，返回 False 时停止（pause/取消协作退出）。
    """
    src = local_model_dir(repo)
    sent = 0
    files = []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        parts = rel.parts
        # 跳过内部标记（锁/临时/未下载记录/分片）
        if any(p in (".locks", ".no_exist") for p in parts):
            continue
        if path.name.endswith((".incomplete", ".lock", ".part")):
            continue
        # 相对路径由 agent 推断控制平面地址；认证走 Authorization 头（token 不进 URL）
        file_url = f"/api/models/files/{repo}?relpath={rel}"
        files.append((path, rel, file_url))

    sem = asyncio.Semaphore(4)
    sent_lock = asyncio.Lock()

    async def pull_one(path, rel, file_url):
        nonlocal sent
        async with sem:
            if should_continue is not None and not await should_continue():
                return  # 任务被暂停/取消：剩余文件不再发送
            if path.is_symlink():
                await agent_client.model_pull(
                    node, repo, str(rel), file_url, 0, symlink=os.readlink(path)
                )
                return
            if not path.is_file():
                return
            size = path.stat().st_size
            for attempt in range(2):  # 重试 1 次：agent .part 断点续传
                try:
                    await agent_client.model_pull(node, repo, str(rel), file_url, size)
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 1:
                        raise
                    await asyncio.sleep(1)
            async with sent_lock:
                sent += size
                await on_progress(sent)

    await asyncio.gather(*(pull_one(p, r, u) for p, r, u in files))
    return sent


# ---------- 阶段 3：head -> worker（RoCE） ----------


async def _sync_to_worker(head: Node, worker: Node, repo: str, info: dict) -> bool:
    roce_ip = _roce_ip(worker) or worker.ip
    try:
        resp = await agent_client.model_sync(head, {
            "target_host": roce_ip,
            "target_user": worker.ssh_username or "spark",
            "target_port": worker.ssh_port,
            "repo": repo,
        })
        info.update(job_id=resp["job_id"], status="syncing")
        while True:
            s = await agent_client.model_sync_status(head, resp["job_id"])
            if s.get("status") == "completed":
                info.update(status="completed")
                return True
            if s.get("status") == "failed":
                info.update(status="failed", error=s.get("error"))
                return False
            await asyncio.sleep(POLL_INTERVAL)
    except Exception as e:  # noqa: BLE001
        info.update(status="failed", error=str(e))
        return False


# ---------- 总编排 ----------


async def start_download_job(repo: str, revision: str, head_node_id: int | None,
                             sync_node_ids: list[int], initial_status: str = "downloading") -> ModelDownload:
    """创建模型传输任务：控制平面下载 ->（可选）发送 head -> RoCE 同步 worker。

    - head_node_id 为 None：仅下载到控制平面缓存，不向任何节点分发
    - initial_status="sending"（分发任务）：跳过下载阶段，直接从本地缓存发送/同步
      （调用方需保证本地模型已完整，见 _verify_local_model）
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
        total = await repo_total_size(repo)
        job = ModelDownload(
            repo=repo,
            revision=revision,
            head_node_id=head_node_id,
            status=initial_status,
            total_bytes=total,
            sync_jobs={str(nid): {"status": "pending"} for nid in sync_node_ids},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        if initial_status == "downloading":
            # 启动控制平面下载线程
            t = threading.Thread(
                target=_start_local_download, args=(job.id, repo, revision), daemon=True
            )
            t.start()
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
        # 完成判定 = 逐文件完整性校验通过（trees 元数据 vs blobs 大小），
        # 不完整绝不进入发送阶段；下载线程失败会把任务置为 failed。
        while job.status == "downloading":
            job.downloaded_bytes = _download_progress(job.repo)
            db.commit()
            if _verify_local_model(job.repo).get("ok"):
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

        # 阶段 2：管理网发送到 head（幂等续传；文件级并发 + 协作暂停）
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

            await _send_repo_to_node(head, job.repo, on_progress, should_continue)
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.error = f"发送到 head 失败: {e}"
            db.commit()
            return
        db.refresh(job)
        if job.status != "sending":
            return  # 发送期间被暂停/取消

        # 阶段 3：head 经 RoCE 同步到各 worker
        job.status = "syncing"
        db.commit()
        all_ok = True
        for nid_str in list((job.sync_jobs or {}).keys()):
            db.refresh(job)
            if job.status != "syncing":
                return  # 同步期间被暂停/取消
            worker = db.get(Node, int(nid_str))
            if not worker:
                job.sync_jobs[nid_str].update(status="failed", error="worker 不存在")
                all_ok = False
                continue
            if not await _sync_to_worker(head, worker, job.repo, job.sync_jobs[nid_str]):
                all_ok = False
            # SQLAlchemy JSON 列不跟踪 dict 原地修改：浅拷贝内容相同，
            # == 比较判等不触发变更检测，必须 flag_modified 强制标记 dirty
            job.sync_jobs = dict(job.sync_jobs)
            flag_modified(job, "sync_jobs")
            db.commit()
        db.refresh(job)
        if job.status != "syncing":
            return

        job.status = "completed" if all_ok else "failed"
        if not all_ok:
            failed = [
                f"#{nid} {j.get('error') or '未知错误'}"[:300]
                for nid, j in (job.sync_jobs or {}).items()
                if j.get("status") == "failed"
            ]
            job.error = "部分 worker 同步失败" + (f"：{'；'.join(failed)}" if failed else "")
        job.downloaded_bytes = local_model_size(job.repo)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("模型任务监控失败 job=%s", job_id)
        db.rollback()  # 异常可能来自 commit/flush（锁冲突等），先回滚再查询
        job = db.get(ModelDownload, job_id)
        if job:
            job.status = "failed"
            job.error = str(e)
            db.commit()
    finally:
        db.close()


async def ensure_model_on_nodes(repo: str, revision: str, nodes: list[Node], head_node_id: int | None = None) -> dict:
    """任务发布前保障模型就绪（控制平面缓存 + head + 各 worker 完整）。

    返回 {"ok": bool, "missing": [...], "download_job_id": int|None, "message": str}
    """
    total = await repo_total_size(repo)
    threshold = total * 0.99 if total else 0
    db = SessionLocal()
    try:
        # 1) 各节点是否已完整缓存（决定能否直接发布）
        node_missing = []
        for n in nodes:
            try:
                st = await agent_client.model_cache_repo(n, repo)
            except Exception:  # noqa: BLE001
                node_missing.append({"where": n.name, "cached": False, "error": "agent 不可达"})
                continue
            cached = bool(st.get("cached"))
            if cached and total:
                cached = (st.get("size_bytes") or 0) >= threshold
            if not cached:
                node_missing.append({"where": n.name, "cached": False})
        if not node_missing:
            return {"ok": True, "missing": [], "download_job_id": None,
                    "message": "模型已完整就绪（全部节点已缓存）"}

        # 2) 有节点缺失 -> 控制平面必须是完整源（下载 -> 管理网发送 head -> RoCE 同步）
        missing = []
        if local_model_size(repo) < threshold:
            missing.append({"where": "控制平面", "cached": False})
        missing += node_missing

        head_id = head_node_id or (nodes[0].id if nodes else None)
        if head_id is None:
            raise ValueError("无可用节点")
        job = await start_download_job(repo, revision, head_id,
                                       [n.id for n in nodes if n.id != head_id])
        return {
            "ok": False,
            "missing": missing,
            "download_job_id": job.id,
            "message": f"模型未完整就绪（{', '.join(m['where'] for m in missing)}），已启动传输任务 #{job.id}（控制平面下载 → 管理网发送 head → RoCE 同步 worker）",
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
            if job.status == "downloading" and not _verify_local_model(job.repo).get("ok"):
                _start_local_download(job.id, job.repo, job.revision)
            spawn(_monitor_job(job.id))
            count += 1
        return count
    finally:
        db.close()
