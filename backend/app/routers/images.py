"""镜像分发 API：registry 拉取 / 管理网发送 head / Agent 高速直传 / 节点加载。

与模型分发同构（方案 A）：解决多节点同时向公网拉镜像的带宽竞争问题。
同 tag 新构建（tag 漂移）的缓存归档会自动识别并重拉；digest 字段展示真实版本。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db, release_db
from ..errors import Code, api_error
from ..models import ImageTransfer, Node
from ..services import agent_client
from ..services.image_manager import (
    IMAGE_CACHE_DIR,
    archive_registry_digest_for,
    image_archive_path,
    image_transfer_to_dict,
    inspect_image,
    start_image_transfer,
)
from ..services.transfer_selection import validate_distribution_cluster

router = APIRouter(prefix="/api/images", tags=["images"])


def get_node_or_404(db: Session, node_id: int) -> Node:
    node = db.get(Node, node_id)
    if not node:
        raise api_error(404, Code.NODE_NOT_FOUND, "节点不存在")
    return node


@router.get("/inspect")
def inspect(image: str):
    """查询镜像元数据（digest/大小/架构），用于校验存在性与进度估算。"""
    try:
        return inspect_image(image)
    except Exception as e:
        raise api_error(422, Code.IMAGE_CHECK_FAILED, f"镜像检查失败: {e}",
                        details=str(e)) from e


class TransferRequest(BaseModel):
    image: str = Field(..., min_length=1)
    cluster_id: int | None = None
    head_node_id: int | None = None   # 缺省 = 仅下载到控制平面
    sync_node_ids: list[int] = Field(default_factory=list)
    force: bool = False               # 强制重新拉取（覆盖已有归档，用于刷新最新版本）


@router.post("/transfer", status_code=201)
async def create_transfer(req: TransferRequest, db: Session = Depends(get_db)):
    """创建镜像传输任务：控制平面拉取 ->（可选）发送 head -> RoCE 同步 -> 节点 docker load。

    force=True 时忽略已有归档强制重新拉取（刷新最新版本）。
    """
    validate_distribution_cluster(db, req.head_node_id, req.sync_node_ids, req.cluster_id)
    try:
        t = await start_image_transfer(req.image, req.head_node_id,
                                       req.sync_node_ids, force=req.force)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return image_transfer_to_dict(t)


@router.get("/transfers")
def list_transfers(status: str | None = None, limit: int | None = None,
                   offset: int = 0, db: Session = Depends(get_db)):
    """镜像传输任务列表（status=active 进行中+失败+暂停+已取消；completed 配合分页）。"""
    q = db.query(ImageTransfer).order_by(ImageTransfer.id.desc())
    if status == "active":
        q = q.filter(ImageTransfer.status.in_(
            ["pulling", "packing", "sending", "syncing", "loading", "failed", "paused", "cancelled"]))
    elif status:
        q = q.filter(ImageTransfer.status == status)
    if limit:
        q = q.limit(min(limit, 100)).offset(max(offset, 0))
    return [image_transfer_to_dict(t) for t in q.all()]


@router.get("/transfers/count")
def count_transfers(status: str, db: Session = Depends(get_db)):
    q = db.query(ImageTransfer)
    if status == "active":
        q = q.filter(ImageTransfer.status.in_(
            ["pulling", "packing", "sending", "syncing", "loading", "failed", "paused", "cancelled"]))
    else:
        q = q.filter(ImageTransfer.status == status)
    return {"count": q.count()}


@router.get("/local")
def list_local(db: Session = Depends(get_db)):
    """控制平面已缓存的镜像归档（含对应的镜像名/digest，供删除/重新拉取）。"""
    # 归档文件名（镜像名哈希）→ 镜像信息映射（从传输任务反查）
    meta: dict[str, dict] = {}
    for t in db.query(ImageTransfer).all():
        p = image_archive_path(t.image, t.digest)
        meta[p.name] = {"image": t.image, "digest": t.digest}
    out = []
    if IMAGE_CACHE_DIR.exists():
        for f in sorted(IMAGE_CACHE_DIR.glob("*.tar"), key=lambda x: -x.stat().st_mtime):
            info = meta.get(f.name, {})
            image = info.get("image")
            out.append({
                "file": f.name,
                "size_bytes": f.stat().st_size,
                "image": image,
                "digest": info.get("digest"),          # 归档文件指纹（分发用）
                "registry_digest": archive_registry_digest_for(image) if image else None,
                "mtime": f.stat().st_mtime,
            })
    return {"cache_dir": str(IMAGE_CACHE_DIR), "archives": out}


@router.delete("/transfers/{job_id}")
async def delete_transfer(job_id: int, db: Session = Depends(get_db)):
    """删除传输任务记录（不影响控制平面归档）。

    进行中的任务先尽力取消后台调度再删除，避免孤儿线程继续写缓存/数据库；
    归档文件按镜像名哈希命名、多任务共享，任务删除不清理归档；
    归档删除走 DELETE /images/local/{file}（归档卡片）。
    """
    from ..services import image_manager

    t = db.get(ImageTransfer, job_id)
    if not t:
        raise api_error(404, Code.IMAGE_TRANSFER_NOT_FOUND, "传输任务不存在")
    if t.status in image_manager._ACTIVE_STATUSES:
        try:
            await image_manager.cancel_image_transfer(job_id)
        except Exception:
            db.rollback()
    t = db.get(ImageTransfer, job_id)
    if t:
        db.delete(t)
        db.commit()
    return {"ok": True, "cleaned_archive": False}


@router.post("/transfers/{job_id}/pause")
async def pause_transfer(job_id: int, db: Session = Depends(get_db)):
    """暂停镜像传输调度；当前文件流保留分片，完成/中断后不再进入下一阶段。"""
    from ..services import image_manager

    if not db.get(ImageTransfer, job_id):
        raise api_error(404, Code.IMAGE_TRANSFER_NOT_FOUND, "传输任务不存在")
    try:
        return await image_manager.pause_image_transfer(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/transfers/{job_id}/resume")
async def resume_transfer(job_id: int, db: Session = Depends(get_db)):
    """继续暂停的传输（回到原阶段；拉取阶段归档未就绪时重启拉取）。"""
    from ..services import image_manager

    if not db.get(ImageTransfer, job_id):
        raise api_error(404, Code.IMAGE_TRANSFER_NOT_FOUND, "传输任务不存在")
    try:
        return await image_manager.resume_image_transfer(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/transfers/{job_id}/cancel")
async def cancel_transfer(job_id: int, db: Session = Depends(get_db)):
    """取消镜像传输调度；当前文件流保留为可复用缓存或续传分片。"""
    from ..services import image_manager

    if not db.get(ImageTransfer, job_id):
        raise api_error(404, Code.IMAGE_TRANSFER_NOT_FOUND, "传输任务不存在")
    try:
        return await image_manager.cancel_image_transfer(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.delete("/local/{file_name}")
def delete_local_archive(file_name: str):
    """删除控制平面本地镜像归档（释放磁盘）。"""
    if not file_name.endswith(".tar") or "/" in file_name or ".." in file_name:
        raise api_error(400, Code.INVALID_FILENAME, "非法文件名")
    path = IMAGE_CACHE_DIR / file_name
    if path.exists():
        path.unlink()
        return {"ok": True, "deleted": file_name}
    return {"ok": True, "deleted": False}


@router.get("/node-status")
async def node_status(image: str, node_id: int, db: Session = Depends(get_db)):
    """指定节点上镜像状态（存在/digest 匹配）。"""
    from ..services.agent_client import map_agent_error

    node = get_node_or_404(db, node_id)
    release_db(db)  # 发布页逐节点轮询本端点：探测离线节点期间不占连接池
    try:
        return await agent_client.image_status(node, image)
    except Exception as e:
        raise map_agent_error(e) from e


class ImageSettingsRequest(BaseModel):
    """镜像拉取设置。docker_proxy: null 清除、字符串覆盖、缺省保持不变。"""

    docker_proxy: str | None = None


@router.get("/settings")
def get_image_settings():
    """镜像拉取设置（代理）。仅影响镜像拉取，不影响模型下载等其他请求。"""
    from ..services.model_manager import get_hf_settings

    return {"docker_proxy": get_hf_settings().get("docker_proxy") or ""}


@router.put("/settings")
def put_image_settings(req: ImageSettingsRequest):
    """更新镜像拉取设置。"""
    from ..services.model_manager import set_hf_settings

    result = set_hf_settings({"docker_proxy": req.docker_proxy}
                             if "docker_proxy" in req.model_dump(exclude_unset=True)
                             else {})
    return {"docker_proxy": result.get("docker_proxy") or ""}
