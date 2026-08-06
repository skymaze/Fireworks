"""节点管理：CRUD、Agent 部署、信息刷新、指标、nvidia-smi、容器代理。"""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..errors import Code, api_error
from ..models import MetricSample, Node, iso_utc
from ..services import agent_client, deploy_agent
from ..services.agent_client import map_agent_error

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def get_node_or_404(db: Session, node_id: int) -> Node:
    node = db.get(Node, node_id)
    if not node:
        raise api_error(404, Code.NODE_NOT_FOUND, "节点不存在")
    return node


@router.get("", response_model=list[schemas.NodeOut])
def list_nodes(
    available: bool = False,
    db: Session = Depends(get_db),
):
    """节点列表；available=true 只返回未加入任何集群的空闲节点。"""
    q = db.query(Node)
    if available:
        q = q.filter(Node.cluster_id.is_(None))
    return q.order_by(Node.id).all()


@router.post("", response_model=schemas.NodeOut, status_code=201)
def create_node(req: schemas.NodeCreate, db: Session = Depends(get_db)):
    if db.query(Node).filter(Node.name == req.name).first():
        raise api_error(409, Code.NODE_NAME_EXISTS, "同名节点已存在")
    node = Node(**req.model_dump())
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


@router.get("/{node_id}", response_model=schemas.NodeOut)
def get_node(node_id: int, db: Session = Depends(get_db)):
    return get_node_or_404(db, node_id)


@router.patch("/{node_id}", response_model=schemas.NodeOut)
def update_node(node_id: int, req: schemas.NodeUpdate, db: Session = Depends(get_db)):
    node = get_node_or_404(db, node_id)
    data = req.model_dump(exclude_unset=True)
    if "name" in data and db.query(Node).filter(
        Node.name == data["name"], Node.id != node_id
    ).first():
        raise api_error(409, Code.NODE_NAME_EXISTS, "同名节点已存在")
    for k, v in data.items():
        setattr(node, k, v)
    db.commit()
    db.refresh(node)
    return node


@router.delete("/{node_id}")
def delete_node(node_id: int, db: Session = Depends(get_db)):
    node = get_node_or_404(db, node_id)
    db.delete(node)
    db.commit()
    return {"ok": True}


@router.post("/{node_id}/deploy-agent")
async def deploy_agent_to_node(node_id: int, db: Session = Depends(get_db)):
    """SSH 上传并部署 Agent 到节点，部署成功后刷新硬件信息。"""
    node = get_node_or_404(db, node_id)
    result = await deploy_agent.deploy(node)
    if result.get("ok"):
        if result.get("hardware_info"):
            node.hardware_info = result["hardware_info"]
            node.agent_status = "online"
            node.last_seen = datetime.now(timezone.utc)
        else:
            node.agent_status = "error"
        db.commit()
    return result


@router.post("/{node_id}/refresh")
async def refresh_node(node_id: int, db: Session = Depends(get_db)):
    """重新拉取 Agent 硬件信息并更新在线状态。"""
    node = get_node_or_404(db, node_id)
    try:
        hw = await agent_client.info(node)
    except Exception as e:  # noqa: BLE001
        node.agent_status = "offline"
        db.commit()
        raise api_error(502, Code.AGENT_UNREACHABLE, f"Agent 不可达: {e}",
                        details=str(e)) from e
    node.hardware_info = hw
    node.agent_status = "online"
    node.last_seen = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "hardware_info": hw}


@router.get("/{node_id}/status")
async def node_status(node_id: int, db: Session = Depends(get_db)):
    node = get_node_or_404(db, node_id)
    ok = await agent_client.health(node)
    if ok:
        node.agent_status = "online"
        node.last_seen = datetime.now(timezone.utc)
    else:
        node.agent_status = "offline"
    db.commit()
    return {
        "id": node.id,
        "agent_status": node.agent_status,
        "last_seen": iso_utc(node.last_seen),
        "docker": (node.hardware_info or {}).get("docker"),
    }


@router.get("/{node_id}/metrics")
def node_metrics(
    node_id: int,
    from_ts: float | None = None,
    to_ts: float | None = None,
    limit: int = 2000,
    db: Session = Depends(get_db),
):
    """节点指标（图表数据），默认最近 1 小时。"""
    get_node_or_404(db, node_id)
    now = time.time()
    to = to_ts if to_ts else now
    frm = from_ts if from_ts else to - 3600
    rows = (
        db.query(MetricSample)
        .filter(
            MetricSample.node_id == node_id,
            MetricSample.ts >= frm,
            MetricSample.ts <= to,
        )
        .order_by(MetricSample.ts)
        .all()
    )
    if limit <= 0:
        # limit=0 时 len(rows)/0 会除零；limit<0 会产生垃圾降采样，统一返回空
        return []
    if len(rows) > limit:
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)]
    return [{"ts": r.ts, "data": r.data} for r in rows]


@router.get("/{node_id}/nvidia-smi")
async def node_nvidia_smi(node_id: int, db: Session = Depends(get_db)):
    node = get_node_or_404(db, node_id)
    try:
        output = await agent_client.nvidia_smi(node)
    except Exception as e:  # noqa: BLE001
        raise api_error(502, Code.NVIDIA_SMI_FAILED, f"nvidia-smi 获取失败: {e}",
                        details=str(e)) from e
    return {"output": output}


@router.get("/{node_id}/containers")
async def node_containers(node_id: int, db: Session = Depends(get_db)):
    node = get_node_or_404(db, node_id)
    try:
        return {"containers": await agent_client.list_containers(node)}
    except Exception as e:  # noqa: BLE001
        raise map_agent_error(e) from e


@router.get("/{node_id}/containers/{name}/logs")
async def node_container_logs(node_id: int, name: str, tail: int = 200, db: Session = Depends(get_db)):
    node = get_node_or_404(db, node_id)
    try:
        logs = await agent_client.container_logs(node, name, tail)
    except Exception as e:  # noqa: BLE001
        raise map_agent_error(e) from e
    return {"node": node.name, "container": name, "logs": logs}


@router.post("/{node_id}/containers/{name}/action")
async def node_container_action(
    node_id: int, name: str, req: schemas.TaskActionRequest, db: Session = Depends(get_db)
):
    node = get_node_or_404(db, node_id)
    action = req.action
    if action == "resume":
        action = "unpause"
    try:
        return await agent_client.container_action(node, name, action)
    except Exception as e:  # noqa: BLE001
        raise map_agent_error(e) from e


@router.get("/{node_id}/models")
async def node_models(node_id: int, db: Session = Depends(get_db)):
    """节点上已接收的模型列表（模型管理）。"""
    node = get_node_or_404(db, node_id)
    try:
        return await agent_client.model_cache(node)
    except Exception as e:  # noqa: BLE001
        raise map_agent_error(e) from e


@router.delete("/{node_id}/models/{repo:path}")
async def node_model_delete(node_id: int, repo: str, db: Session = Depends(get_db)):
    """删除节点上的指定模型。"""
    node = get_node_or_404(db, node_id)
    try:
        return await agent_client.model_delete(node, repo)
    except Exception as e:  # noqa: BLE001
        raise map_agent_error(e) from e
