"""模型管理 API：HF 搜索 / 控制平面下载 / 管理网发送 / 节点模型管理。"""

from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import ModelDownload, Node
from ..services import agent_client
from ..services.model_manager import (
    get_hf_settings,
    job_to_dict,
    local_model_dir,
    local_model_size,
    set_hf_settings,
    start_download_job,
)

router = APIRouter(prefix="/api/models", tags=["models"])


class SettingsRequest(BaseModel):
    """下载配置更新。endpoint/connections/chunk_size_mb/docker_proxy: 传 null 或省略保持不变；
    hf_token: 传 null 清除、字符串覆盖、省略保持。"""

    endpoint: str | None = None
    hf_token: str | None = None
    connections: int | None = Field(default=None, ge=1, le=32)
    chunk_size_mb: int | None = Field(default=None, ge=1, le=64)
    docker_proxy: str | None = None   # 镜像拉取代理（http://host:port，skopeo 使用）


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

HF_API = "https://huggingface.co/api"


def get_node_or_404(db: Session, node_id: int) -> Node:
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "节点不存在")
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
            raise HTTPException(404, "模型不存在")
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
async def cached_model(repo: str, node_id: int, db: Session = Depends(get_db)):
    """指定节点上指定模型的缓存状态。"""
    from ..services.agent_client import map_agent_error

    node = get_node_or_404(db, node_id)
    try:
        return await agent_client.model_cache_repo(node, repo)
    except Exception as e:  # noqa: BLE001
        raise map_agent_error(e) from e


def _local_model_status(db: Session, repo: str) -> str:
    """控制平面模型缓存状态机：
    complete    - 逐文件校验通过（可分发/可正常删除）
    downloading - 该模型有进行中的传输任务（禁止删除，避免误删下载中的缓存）
    failed      - 最近一次任务失败且无进行中任务（可删除残留）
    partial     - 存在残留文件但从未成功/校验失败（中断残留，可删除）
    """
    from ..services.model_manager import _verify_local_model

    if _verify_local_model(repo)["ok"]:
        return "complete"
    active = db.query(ModelDownload).filter(
        ModelDownload.repo == repo,
        # 含 paused：暂停的下载仍视为"下载中"（可继续），避免误判为残留
        ModelDownload.status.in_(["downloading", "sending", "syncing", "paused"]),
    ).first()
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
    """控制平面本地已下载的模型列表（含多态状态 status）。

    status: complete | downloading | failed | partial
    """
    cache = Path(config.MODEL_CACHE_DIR)
    out = []
    if cache.exists():
        for d in sorted(cache.glob("models--*")):
            repo = d.name[len("models--"):].replace("--", "/", 1).replace("--", "-")
            status = _local_model_status(db, repo)
            out.append({
                "repo": repo,
                "size_bytes": local_model_size(repo),
                "complete": status == "complete",
                "status": status,
            })
    return {"cache_dir": str(cache), "models": out}


@router.delete("/local/{repo:path}")
def delete_local_model(repo: str):
    """删除控制平面本地模型缓存（释放磁盘）。"""
    import shutil

    d = local_model_dir(repo)
    if d.exists():
        shutil.rmtree(d)
        return {"ok": True, "repo": repo, "deleted": True}
    return {"ok": True, "repo": repo, "deleted": False}


class DownloadRequest(BaseModel):
    repo: str = Field(..., min_length=1)
    revision: str = "main"
    # 缺省 = 仅下载到控制平面，不分发节点
    head_node_id: int | None = None
    sync_node_ids: list[int] = Field(default_factory=list)


@router.post("/download", status_code=201)
async def start_download(req: DownloadRequest, db: Session = Depends(get_db)):
    """启动模型传输：控制平面下载 -> 管理网发送 head -> RoCE 同步 sync 节点。

    head_node_id 缺省时仅下载到控制平面（不分发节点）。
    """
    if req.head_node_id is not None:
        get_node_or_404(db, req.head_node_id)
    for nid in req.sync_node_ids:
        get_node_or_404(db, nid)
    try:
        job = await start_download_job(req.repo, req.revision, req.head_node_id, req.sync_node_ids)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return job_to_dict(job)


@router.post("/distribute", status_code=201)
async def start_distribute(req: DownloadRequest, db: Session = Depends(get_db)):
    """仅分发：把控制平面本地已完整缓存的模型发送到 head 并经 RoCE 同步 worker。

    与下载解耦：模型先在管理平面下载完成（/download，head_node_id 缺省），
    之后任意时刻可对任意节点组合发起分发（本地缓存不完整时返回 409）。
    """
    from ..services.model_manager import _verify_local_model

    if req.head_node_id is None:
        raise HTTPException(422, "分发必须指定 head 节点")
    get_node_or_404(db, req.head_node_id)
    for nid in req.sync_node_ids:
        get_node_or_404(db, nid)
    v = _verify_local_model(req.repo)
    if not v["ok"]:
        raise HTTPException(409, f"本地缓存不完整，无法分发：{v['error']}；请先执行「仅下载」或「下载并分发」")
    try:
        job = await start_download_job(req.repo, req.revision, req.head_node_id,
                                       req.sync_node_ids, initial_status="sending")
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return job_to_dict(job)


class SyncRequest(BaseModel):
    repo: str = Field(..., min_length=1)
    revision: str = "main"
    from_node_id: int  # 模型来源（head）
    to_node_id: int    # 同步目标（worker）


@router.post("/sync")
async def manual_sync(req: SyncRequest, db: Session = Depends(get_db)):
    """手动把 from 节点上的模型缓存同步到 to 节点（走 RoCE 高速网）。"""
    src = get_node_or_404(db, req.from_node_id)
    dst = get_node_or_404(db, req.to_node_id)
    hw = dst.hardware_info or {}
    roce_ip = None
    for r in hw.get("roce") or []:
        if r.get("rocev2_ip"):
            roce_ip = r["rocev2_ip"]
            break
    resp = await agent_client.model_sync(src, {
        "target_host": roce_ip or dst.ip,
        "target_user": dst.ssh_username or "spark",
        "target_port": dst.ssh_port,
        "repo": req.repo,
        "revision": req.revision,
    })
    return {"job_id": resp["job_id"], "from": src.name, "to": dst.name,
            "target_host": roce_ip or dst.ip}


@router.get("/sync/{job_id}")
async def sync_status(job_id: str, from_node_id: int, db: Session = Depends(get_db)):
    """查询手动同步任务进度（需 from 节点）。"""
    from ..services.agent_client import map_agent_error

    src = get_node_or_404(db, from_node_id)
    try:
        return await agent_client.model_sync_status(src, job_id)
    except Exception as e:  # noqa: BLE001
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
def batch_delete_downloads(req: BatchDeleteRequest, db: Session = Depends(get_db)):
    """批量删除任务记录；cleanup=True 时顺带清理各模型残留临时文件。"""
    deleted = 0
    cleaned = 0
    for jid in req.ids:
        job = db.get(ModelDownload, jid)
        if not job:
            continue
        repo = job.repo
        db.delete(job)
        db.commit()
        deleted += 1
        if req.cleanup:
            cleaned += _cleanup_repo_residue(db, repo)
    return {"ok": True, "deleted": deleted, "cleaned_files": cleaned}


@router.post("/downloads/{job_id}/retry")
async def retry_download(job_id: int, db: Session = Depends(get_db)):
    """重试失败任务：按原任务参数（repo/head/workers）重新发起。

    仅下载任务（head 为空）重试仍只下载控制平面；已下载的分片/blobs 断点续传。
    """
    job = db.get(ModelDownload, job_id)
    if not job:
        raise HTTPException(404, "下载任务不存在")
    if job.status != "failed":
        raise HTTPException(409, "只有失败的任务可以重试")
    if job.head_node_id is not None:
        get_node_or_404(db, job.head_node_id)
    sync_ids = [int(k) for k in (job.sync_jobs or {}).keys()]
    for nid in sync_ids:
        get_node_or_404(db, nid)
    try:
        new_job = await start_download_job(job.repo, job.revision,
                                           job.head_node_id, sync_ids)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return job_to_dict(new_job)


@router.get("/downloads/{job_id}")
def get_download(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ModelDownload, job_id)
    if not job:
        raise HTTPException(404, "下载任务不存在")
    return job_to_dict(job)


@router.post("/downloads/{job_id}/pause")
async def pause_download(job_id: int, db: Session = Depends(get_db)):
    """暂停下载/分发任务（下载线程在分片边界退出，已下载分片保留可续传）。"""
    from ..services import model_manager

    if not db.get(ModelDownload, job_id):
        raise HTTPException(404, "下载任务不存在")
    try:
        return await model_manager.pause_download(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/downloads/{job_id}/resume")
async def resume_download(job_id: int, db: Session = Depends(get_db)):
    """继续暂停的任务（分片续传 / 幂等重跑发送同步）。"""
    from ..services import model_manager

    if not db.get(ModelDownload, job_id):
        raise HTTPException(404, "下载任务不存在")
    try:
        return await model_manager.resume_download(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/downloads/{job_id}/cancel")
async def cancel_download(job_id: int, db: Session = Depends(get_db)):
    """取消下载/分发任务（停止下载线程；分片保留，可重试续传）。"""
    from ..services import model_manager

    if not db.get(ModelDownload, job_id):
        raise HTTPException(404, "下载任务不存在")
    try:
        return await model_manager.cancel_download(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.delete("/downloads/{job_id}")
def delete_download(job_id: int, cleanup: int = 0, db: Session = Depends(get_db)):
    """删除下载任务记录；cleanup=1 时顺带清理该模型的下载残留临时文件。

    只清理 .incomplete / .part / .lock（中断残留），已完成的 blobs 保留
    （可继续分发/复用）。若该模型仍有进行中的任务则不清理，避免误删。
    """
    job = db.get(ModelDownload, job_id)
    if not job:
        raise HTTPException(404, "下载任务不存在")
    repo = job.repo
    db.delete(job)
    db.commit()
    cleaned = _cleanup_repo_residue(db, repo) if cleanup else 0
    return {"ok": True, "cleaned_files": cleaned}
