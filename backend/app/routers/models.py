"""模型管理 API：HF 搜索 / 控制平面下载 / 管理网发送 / 节点模型管理。"""

import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db, release_db
from ..errors import Code, api_error
from ..models import ModelDownload, Node
from ..services import agent_client, peer_transfer
from ..services.model_manager import (
    get_hf_settings,
    job_to_dict,
    local_model_dir,
    local_model_size,
    set_hf_settings,
    start_download_job,
)
from ..services.transfer_selection import validate_distribution_cluster

router = APIRouter(prefix="/api/models", tags=["models"])


class SettingsRequest(BaseModel):
    """下载配置更新。endpoint/connections/chunk_size_mb/docker_proxy: 传 null 或省略保持不变；
    hf_token: 传 null 清除、字符串覆盖、省略保持。"""

    endpoint: str | None = None
    hf_token: str | None = None
    connections: int | None = Field(default=None, ge=1, le=32)
    chunk_size_mb: int | None = Field(default=None, ge=1, le=64)
    docker_proxy: str | None = None   # 镜像 registry 拉取代理（http/https/socks）


@router.get("/settings")
def get_settings():
    """模型下载配置（endpoint 镜像源 / token 是否已配置 / 连接数 / 分片大小）。"""
    return get_hf_settings()


@router.put("/settings")
def put_settings(req: SettingsRequest):
    """更新模型下载配置（token 不参与回显，只返回是否已配置）。

    保存后重启所有进行中的下载任务，使新设置（endpoint/token/连接数/分片）
    立即生效；已下载的分片/blobs 断点续传，不重复下载。
    """
    from ..services.model_manager import restart_downloads_with_new_settings

    result = set_hf_settings(req.model_dump(exclude_unset=True))
    restarted = restart_downloads_with_new_settings()
    result["restarted_downloads"] = restarted
    return result


def get_node_or_404(db: Session, node_id: int) -> Node:
    node = db.get(Node, node_id)
    if not node:
        raise api_error(404, Code.NODE_NOT_FOUND, "节点不存在")
    return node


@router.get("/search")
async def search_models(q: str = "", limit: int = 12):
    """在 Hugging Face 搜索模型（使用下载配置的 endpoint）。"""
    from ..services.model_manager import get_hf_settings

    s = get_hf_settings()
    base = s["endpoint"].rstrip("/") + "/api"
    params: dict = {"limit": min(limit, 50)}
    if q:
        params["search"] = q
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(f"{base}/models", params=params)
        r.raise_for_status()
        data = r.json()
    return [
        {
            "id": m.get("id"),
            "likes": m.get("likes"),
            "downloads": m.get("downloads"),
            "lastModified": m.get("lastModified"),
            "pipeline_tag": m.get("pipeline_tag"),
        }
        for m in data
    ]


@router.get("/{repo:path}/info")
async def model_info(repo: str):
    """模型详情（含各文件大小，用于进度估算）。"""
    from ..services.model_manager import get_hf_settings

    s = get_hf_settings()
    base = s["endpoint"].rstrip("/") + "/api"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(f"{base}/models/{repo}?blobs=true")
        # hf-mirror 对不存在的仓库返回 401（Invalid username or password），
        # 官方源返回 404——统一视为模型不存在
        if r.status_code in (404, 401):
            raise api_error(404, Code.MODEL_NOT_FOUND, "模型不存在")
        r.raise_for_status()
        data = r.json()
    siblings = []
    total = 0
    for s in data.get("siblings", []):
        sz = s.get("size") or 0
        total += sz
        siblings.append({"name": s.get("rfilename"), "size": sz})
    return {
        "id": data.get("id"),
        "gated": data.get("gated"),
        "likes": data.get("likes"),
        "downloads": data.get("downloads"),
        "lastModified": data.get("lastModified"),
        "total_size": total,
        "siblings": siblings,
    }


@router.get("/cached/{repo:path}")
async def cached_model(repo: str, node_id: int, sha: str | None = None,
                       db: Session = Depends(get_db)):
    """指定节点上指定模型的缓存状态；sha 给定则精确校验该 commit 版本（版本钉扎）。"""
    from ..services.agent_client import map_agent_error

    if sha is not None:
        sha = str(sha).strip().lower()
        if not sha or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
            raise HTTPException(422, "sha 非法：应为 git commit 十六进制哈希")
    node = get_node_or_404(db, node_id)
    release_db(db)  # 发布页逐节点轮询本端点：探测离线节点期间不占连接池
    try:
        return await agent_client.model_cache_repo(node, repo, sha=sha)
    except Exception as e:
        raise map_agent_error(e) from e


def _local_model_status(db: Session, repo: str) -> str:
    """控制平面模型缓存状态机：
    complete    - 逐文件校验通过（可分发/可正常删除）
    downloading - 有进行中的控制平面下载任务（禁止删除，避免误删下载中的缓存）
    sending     - 正在发送到 head（分发进行中）
    syncing     - head 正在向 worker 高速直传（分发进行中）
    failed      - 最近一次任务失败且无进行中任务（可删除残留）
    partial     - 存在残留文件但从未成功/校验失败（中断残留，可删除）
    """
    from ..services.model_manager import _verify_local_model

    # 先看进行中的传输任务：sending/syncing 即「分发进行中」，直接呈现阶段，
    # 而不是笼统标成 downloading（下载中）——否则完整性校验失败的缓存会在
    # 分发期间被误标为「下载中」（同时禁用删除按钮），与实际行为不符。
    active = db.query(ModelDownload).filter(
        ModelDownload.repo == repo,
        # 含 paused：暂停的任务仍视为传输中（可继续/可删除前需取消）
        ModelDownload.status.in_(["downloading", "sending", "syncing", "paused"]),
    ).first()
    if active and active.status in ("sending", "syncing"):
        return active.status
    if _verify_local_model(repo)["ok"]:
        return "complete"
    if active:
        return "downloading"
    latest = db.query(ModelDownload).filter(
        ModelDownload.repo == repo
    ).order_by(ModelDownload.id.desc()).first()
    if latest and latest.status == "failed":
        return "failed"
    return "partial"


@router.get("/local")
def local_models(db: Session = Depends(get_db)):
    """控制平面本地已下载的模型列表（含多态状态 status 与版本元数据）。

    status: complete | downloading | sending | syncing | failed | partial
    版本元数据：active_sha/revision 为当前激活版本，versions 列出缓存目录里
    保留的全部 commit（新版本下载/切换后旧版本仍在，零成本回滚）。
    """
    from ..services.model_manager import _active_snapshot, _snapshot_versions

    cache = Path(config.MODEL_CACHE_DIR)
    out = []
    if cache.exists():
        for d in sorted(cache.glob("models--*")):
            repo = d.name[len("models--"):].replace("--", "/", 1).replace("--", "-")
            status = _local_model_status(db, repo)
            revision, active_sha = _active_snapshot(repo)
            out.append({
                "repo": repo,
                "size_bytes": local_model_size(repo),
                "complete": status == "complete",
                "status": status,
                "revision": revision,
                "active_sha": active_sha,
                "versions": _snapshot_versions(repo),
            })
    return {"cache_dir": str(cache), "models": out}


@router.delete("/local/{repo:path}")
def delete_local_model(repo: str, db: Session = Depends(get_db)):
    """删除控制平面本地模型缓存（释放磁盘）。

    该模型仍有进行中的下载/分发任务时拒绝删除，避免 rm -rf 与后台下载线程
    双写同一缓存目录（孤儿线程写坏数据）。请先取消或等待任务结束。
    """
    import shutil

    active = db.query(ModelDownload).filter(
        ModelDownload.repo == repo,
        ModelDownload.status.in_(["downloading", "sending", "syncing", "paused"]),
    ).first()
    if active:
        raise api_error(
            409, Code.MODEL_BUSY,
            f"模型 {repo} 有进行中的任务 #{active.id}（{active.status}），请先取消或等待完成",
        )
    d = local_model_dir(repo)
    if d.exists():
        shutil.rmtree(d)
        return {"ok": True, "repo": repo, "deleted": True}
    return {"ok": True, "repo": repo, "deleted": False}


class DownloadRequest(BaseModel):
    repo: str = Field(..., min_length=1)
    revision: str = "main"
    # 显式目标 commit（版本切换/按版本分发）：给定且本地完整时直接以该版本为目标；
    # downloading 模式下缺失会按该 sha 续传补齐，分发（sending）模式必须本地完整。
    sha: str | None = None
    # force：即使目标 revision 缓存已完整也重新解析远端并增量补齐（「更新到最新」）
    force: bool = False
    cluster_id: int | None = None
    # 缺省 = 仅下载到控制平面，不分发节点
    head_node_id: int | None = None
    sync_node_ids: list[int] = Field(default_factory=list)

    @field_validator("revision")
    @classmethod
    def _check_revision(cls, v: str) -> str:
        # 入口校验：revision 会落到本地缓存路径（refs/）与远端 URL，拒绝路径越级
        from ..services.model_manager import validate_revision

        return validate_revision(v)

    @field_validator("sha")
    @classmethod
    def _check_sha(cls, v: str | None) -> str | None:
        # commit sha 只会作为 refs 文件名的 path segment 与比对键（不自持长度），
        # 拒绝路径越级；hex git 全 sha 为 40 位，放宽格式校验避免误伤。
        if v is None:
            return None
        v = str(v).strip()
        if not v or not re.fullmatch(r"[0-9a-fA-F]{7,64}", v):
            raise ValueError("sha 非法：应为 git commit 十六进制哈希")
        return v.lower()


@router.post("/download", status_code=201)
async def start_download(req: DownloadRequest, db: Session = Depends(get_db)):
    """启动模型传输：控制平面下载 -> 管理网发送 head -> Agent 高速直传 worker。

    head_node_id 缺省时仅下载到控制平面（不分发节点）；
    sha 给定：按该版本下载/续传；force：缓存完整也强制刷新到最新。
    """
    validate_distribution_cluster(db, req.head_node_id, req.sync_node_ids, req.cluster_id)
    try:
        job = await start_download_job(req.repo, req.revision, req.head_node_id,
                                       req.sync_node_ids, sha=req.sha, force=req.force)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return job_to_dict(job)


@router.post("/distribute", status_code=201)
async def start_distribute(req: DownloadRequest, db: Session = Depends(get_db)):
    """仅分发：把控制平面完整缓存发送到 head，再由 worker Agent 高速直拉。

    与下载解耦：模型先在管理平面下载完成（/download，head_node_id 缺省），
    之后任意时刻可对同一集群内的节点组合发起分发（本地缓存不完整时返回 409）。
    sha 给定：分发该版本（必须是本地已完整缓存的版本，即「按版本切换分发」）。
    """
    from ..services.model_manager import _verify_local_model

    if req.head_node_id is None:
        raise api_error(422, Code.DISTRIBUTE_HEAD_REQUIRED, "分发必须指定 head 节点")
    validate_distribution_cluster(db, req.head_node_id, req.sync_node_ids, req.cluster_id)
    if req.sha:
        from ..services.model_manager import _verify_snapshot

        v = _verify_snapshot(req.repo, req.sha)
        if not v["ok"]:
            raise api_error(409, Code.LOCAL_CACHE_INCOMPLETE,
                            f"目标版本 {req.sha[:12]} 本地缓存不完整，无法按该版本分发；"
                            f"请先切换到该版本/重新下载：{v['error']}",
                            details=v.get("error"))
    else:
        v = _verify_local_model(req.repo)
        if not v["ok"]:
            raise api_error(409, Code.LOCAL_CACHE_INCOMPLETE,
                            f"本地缓存不完整，无法分发：{v['error']}；请先下载模型",
                            details=v.get("error"))
    try:
        job = await start_download_job(req.repo, req.revision, req.head_node_id,
                                       req.sync_node_ids, initial_status="sending",
                                       sha=req.sha, force=req.force)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return job_to_dict(job)


class SyncRequest(BaseModel):
    repo: str = Field(..., min_length=1)
    from_node_id: int  # 模型来源（head）
    to_node_id: int    # 同步目标（worker）


@router.post("/sync")
async def manual_sync(req: SyncRequest, db: Session = Depends(get_db)):
    """手动让目标 Agent 经高速网从来源 Agent 直拉模型（无 SSH/rsync）。"""
    src = get_node_or_404(db, req.from_node_id)
    dst = get_node_or_404(db, req.to_node_id)
    if src.id == dst.id:
        raise HTTPException(422, "模型来源和目标节点不能相同")
    validate_distribution_cluster(db, src.id, [dst.id])
    release_db(db)  # 能力探测 + 直拉是长网络操作，期间不占连接池（会话可复用）
    for node in (src, dst):
        error = await peer_transfer.check_agent_capability(
            node, agent_client, "model_peer_transfer_v1",
        )
        if error:
            raise HTTPException(502, error)
    share = await agent_client.model_share(src, req.repo)
    source_host = peer_transfer.node_transfer_ip(db, src)
    source_url = f"http://{source_host}:{src.agent_port}{share['path']}"
    resp = await agent_client.model_fetch(dst, {
        "source_url": source_url,
        "source_token": share["token"],
        "repo": req.repo,
        "manifest": share["manifest"],
        "total_size": share["total_size"],
        "transfer_id": 0,
        "connections": 4,
    })
    # 将目标节点写入公开任务 ID，后端重启后仍能定位真正执行 fetch 的 Agent。
    public_job_id = f"{dst.id}:{resp['job_id']}"
    return {"job_id": public_job_id, "from": src.name, "to": dst.name,
            "source_host": source_host, "transport": "high_speed_http"}


@router.get("/sync/{job_id}")
async def sync_status(job_id: str, db: Session = Depends(get_db)):
    """查询手动同步任务进度；任务 ID 格式为 <目标节点 ID>:<Agent job ID>。"""
    from ..services.agent_client import map_agent_error

    node_id_text, separator, agent_job_id = job_id.partition(":")
    if not (separator and node_id_text.isdigit() and agent_job_id):
        raise HTTPException(422, "同步任务 ID 格式无效")
    node = get_node_or_404(db, int(node_id_text))
    release_db(db)  # 传输进度被前端周期轮询：探测离线节点期间不占连接池
    try:
        return await agent_client.model_fetch_status(node, agent_job_id)
    except Exception as e:
        raise map_agent_error(e) from e


@router.get("/downloads/count")
def count_downloads(status: str, db: Session = Depends(get_db)):
    """按状态统计任务数（折叠标题用，避免分页加载才能看到计数）。"""
    q = db.query(ModelDownload)
    if status == "active":
        q = q.filter(ModelDownload.status.in_(["downloading", "sending", "syncing", "failed"]))
    else:
        q = q.filter(ModelDownload.status == status)
    return {"count": q.count()}


@router.get("/downloads")
def list_downloads(status: str | None = None, limit: int | None = None,
                   offset: int = 0, db: Session = Depends(get_db)):
    """任务列表。status=active 返回进行中+失败+暂停+已取消；status=completed 配合 limit/offset 分页。"""
    q = db.query(ModelDownload).order_by(ModelDownload.id.desc())
    if status == "active":
        q = q.filter(ModelDownload.status.in_(
            ["downloading", "sending", "syncing", "failed", "paused", "cancelled"]))
    elif status:
        q = q.filter(ModelDownload.status == status)
    if limit:
        q = q.limit(min(limit, 100)).offset(max(offset, 0))
    return [job_to_dict(j) for j in q.all()]


def _cleanup_repo_residue(db: Session, repo: str) -> int:
    """清理某模型缓存目录的下载残留临时文件（该 repo 无进行中任务时）。

    只清理 .incomplete / .part / .lock，已完成的 blobs 保留（可分发/复用）。
    """
    from ..services.model_manager import local_model_dir

    active = db.query(ModelDownload).filter(
        ModelDownload.repo == repo,
        ModelDownload.status.in_(["downloading", "sending", "syncing", "paused"]),
    ).first()
    if active:
        return 0
    blobs = local_model_dir(repo) / "blobs"
    cleaned = 0
    if blobs.exists():
        for f in blobs.iterdir():
            if (f.name.endswith((".incomplete", ".lock")) or ".part." in f.name):
                try:
                    f.unlink()
                    cleaned += 1
                except OSError:
                    pass
    return cleaned


class BatchDeleteRequest(BaseModel):
    ids: list[int]
    cleanup: bool = False


@router.delete("/downloads/all-completed")
def delete_all_completed(cleanup: int = 0, db: Session = Depends(get_db)):
    """一键清空所有已完成任务记录；cleanup=1 时顺带清理各模型残留临时文件。"""
    jobs = db.query(ModelDownload).filter(ModelDownload.status == "completed").all()
    deleted = 0
    cleaned = 0
    for job in jobs:
        repo = job.repo
        db.delete(job)
        db.commit()
        deleted += 1
        if cleanup:
            cleaned += _cleanup_repo_residue(db, repo)
    return {"ok": True, "deleted": deleted, "cleaned_files": cleaned}


@router.post("/downloads/batch-delete")
async def batch_delete_downloads(req: BatchDeleteRequest, db: Session = Depends(get_db)):
    """批量删除任务记录；cleanup=True 时顺带清理各模型残留临时文件。

    进行中的任务先取消后台调度再删除，避免孤儿线程继续写缓存/数据库。
    """
    from ..services import model_manager

    deleted = 0
    cleaned = 0
    for jid in req.ids:
        job = db.get(ModelDownload, jid)
        if not job:
            continue
        repo = job.repo
        if job.status in model_manager._ACTIVE_STATUSES:
            try:
                await model_manager.cancel_download(jid)
            except Exception:
                db.rollback()
        job = db.get(ModelDownload, jid)
        if not job:
            continue
        db.delete(job)
        db.commit()
        deleted += 1
        if req.cleanup:
            cleaned += _cleanup_repo_residue(db, repo)
    return {"ok": True, "deleted": deleted, "cleaned_files": cleaned}


@router.post("/downloads/{job_id}/retry")
async def retry_download(job_id: int, db: Session = Depends(get_db)):
    """重试失败任务：就地复活原任务（同一 job_id），不新建任务记录。

    失败任务回到 downloading 阶段重启：已下载的分片/blobs 断点续传，
    监控幂等推进发送/同步阶段。重试后 UI 上仍是同一条任务，不会出现
    「旧的失败记录 + 新的下载记录」并存、用户误以为又失败一次。
    """
    from ..services import model_manager

    job = db.get(ModelDownload, job_id)
    if not job:
        raise api_error(404, Code.MODEL_DOWNLOAD_NOT_FOUND, "下载任务不存在")
    if job.status != "failed":
        raise api_error(409, Code.RETRY_ONLY_FAILED, "只有失败的任务可以重试")
    if job.head_node_id is not None:
        get_node_or_404(db, job.head_node_id)
    sync_ids = [int(k) for k in (job.sync_jobs or {}).keys()]
    for nid in sync_ids:
        get_node_or_404(db, nid)
    validate_distribution_cluster(db, job.head_node_id, sync_ids)
    try:
        job = await model_manager.retry_download_job(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return job_to_dict(job)


@router.get("/downloads/{job_id}")
def get_download(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ModelDownload, job_id)
    if not job:
        raise api_error(404, Code.MODEL_DOWNLOAD_NOT_FOUND, "下载任务不存在")
    return job_to_dict(job)


@router.post("/downloads/{job_id}/pause")
async def pause_download(job_id: int, db: Session = Depends(get_db)):
    """暂停下载/分发任务（下载线程在分片边界退出，已下载分片保留可续传）。"""
    from ..services import model_manager

    if not db.get(ModelDownload, job_id):
        raise api_error(404, Code.MODEL_DOWNLOAD_NOT_FOUND, "下载任务不存在")
    try:
        return await model_manager.pause_download(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/downloads/{job_id}/resume")
async def resume_download(job_id: int, db: Session = Depends(get_db)):
    """继续暂停的任务（分片续传 / 幂等重跑发送同步）。"""
    from ..services import model_manager

    if not db.get(ModelDownload, job_id):
        raise api_error(404, Code.MODEL_DOWNLOAD_NOT_FOUND, "下载任务不存在")
    try:
        return await model_manager.resume_download(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/downloads/{job_id}/cancel")
async def cancel_download(job_id: int, db: Session = Depends(get_db)):
    """取消下载/分发任务（停止下载线程；分片保留，可重试续传）。"""
    from ..services import model_manager

    if not db.get(ModelDownload, job_id):
        raise api_error(404, Code.MODEL_DOWNLOAD_NOT_FOUND, "下载任务不存在")
    try:
        return await model_manager.cancel_download(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.delete("/downloads/{job_id}")
async def delete_download(job_id: int, cleanup: int = 0, db: Session = Depends(get_db)):
    """删除下载任务记录；cleanup=1 时顺带清理该模型的下载残留临时文件。

    只清理 .incomplete / .part / .lock（中断残留），已完成的 blobs 保留
    （可继续分发/复用）。若该模型仍有进行中的任务则不清理，避免误删。
    进行中的任务先取消后台调度再删除记录，避免孤儿线程继续写缓存/数据库。
    """
    from ..services import model_manager

    job = db.get(ModelDownload, job_id)
    if not job:
        raise api_error(404, Code.MODEL_DOWNLOAD_NOT_FOUND, "下载任务不存在")
    repo = job.repo
    if job.status in model_manager._ACTIVE_STATUSES:
        try:
            await model_manager.cancel_download(job_id)
        except Exception:
            db.rollback()
    job = db.get(ModelDownload, job_id)
    if job:
        db.delete(job)
        db.commit()
    cleaned = _cleanup_repo_residue(db, repo) if cleanup else 0
    return {"ok": True, "cleaned_files": cleaned}


class PruneRequest(BaseModel):
    keep: int = Field(default=3, ge=0, le=50)


@router.post("/{repo:path}/prune")
def prune_versions(repo: str, req: PruneRequest, db: Session = Depends(get_db)):
    """清理历史版本（GC）：删除不被引用、且非最新 keep 个完整版本的快照。

    被 refs/激活版本/进行中任务引用的版本始终保留；blobs 内容寻址去重、不删除。
    返回被清理的 commit 列表。
    """
    from ..services.model_manager import prune_repo_versions

    if not local_model_dir(repo).exists():
        raise api_error(404, Code.MODEL_NOT_FOUND, "模型缓存不存在")
    deleted = prune_repo_versions(repo, req.keep)
    return {"ok": True, "repo": repo, "deleted": deleted, "count": len(deleted)}
