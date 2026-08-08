"""内部端点（Agent 回拉）：供节点 Agent 从控制平面拉取模型文件/镜像归档。

仅这两个端点使用「登录会话 或 Agent 共享 token」双门控（get_user_or_agent）：
- 登录用户可访问（兼容前端现有用法）；
- 节点 Agent 携带共享 token（env AGENT_TOKEN 或自动生成持久化）访问，
  使模型分发/镜像传输在控制平面启用认证后仍不中断，且无需重部署节点 Agent。

其余所有业务端点均要求登录（见 main.py 注册时的认证依赖）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import Code, api_error
from ..models import ImageTransfer
from ..security import get_user_or_agent
from ..services.image_manager import image_archive_path
from ..services.model_manager import local_model_dir

router = APIRouter(prefix="/api", tags=["internal"])


@router.get(
    "/models/files/{repo:path}",
    dependencies=[Depends(get_user_or_agent)],
)
def model_file(repo: str, relpath: str):
    """控制平面本地模型文件（供 agent 经管理网拉取，GET 流式）。"""
    base = local_model_dir(repo).resolve()
    target = (base / relpath).resolve()
    if not str(target).startswith(str(base) + "/"):
        raise api_error(400, Code.INVALID_RELPATH, "非法 relpath")
    if not target.exists():
        raise api_error(404, Code.FILE_NOT_FOUND, "文件不存在")
    return FileResponse(target, filename=target.name)


@router.get(
    "/images/archive/{job_id}",
    dependencies=[Depends(get_user_or_agent)],
)
def get_archive(job_id: int, db: Session = Depends(get_db)):
    """提供镜像归档文件（agent 管理网拉取用，GET 流式）。"""
    t = db.get(ImageTransfer, job_id)
    if not t:
        raise api_error(404, Code.IMAGE_TRANSFER_NOT_FOUND, "传输任务不存在")
    path = image_archive_path(t.image, t.digest)
    if not path.exists():
        raise api_error(404, Code.ARCHIVE_NOT_FOUND, "归档文件不存在（可能尚未拉取完成）")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")
