"""节点管理：CRUD、Agent 部署/卸载、信息刷新、指标、nvidia-smi、容器代理。"""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..errors import Code, api_error
from ..models import (
    Cluster,
    ClusterNode,
    MetricSample,
    Node,
    Task,
    TaskNode,
    iso_utc,
)
from ..services import agent_client, agent_ws, deploy_agent, node_optimize, ssh_client
from ..services import network_config as network_config_svc
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
async def create_node(req: schemas.NodeCreate, db: Session = Depends(get_db)):
    """添加节点即安装 Agent：节点入库后立即 SSH 部署并验证可达。

    只有 Agent 安装成功且连通性验证通过才算添加成功；安装/验证任一失败都
    明确报错并回滚（卸载已部署的 Agent + 删除节点行），不留下不可达的半成品节点。
    部署成功后（默认）执行「初始优化」——关闭 Wi-Fi/蓝牙、关闭 GUI、授予 docker
    权限、关闭 swap。优化为 best-effort：失败/警告不阻断添加，结果落 optimize_result。
    """
    if db.query(Node).filter(Node.name == req.name).first():
        raise api_error(409, Code.NODE_NAME_EXISTS, "同名节点已存在")
    data = req.model_dump()
    optimize_on_add = data.pop("optimize_on_add", True)  # 非节点表字段，先弹出
    node = Node(**data)
    db.add(node)
    db.commit()
    db.refresh(node)
    try:
        await _install_agent_when_creating(node)
    except Exception:
        db.delete(node)
        db.commit()
        raise
    if optimize_on_add:
        node.optimize_result = await _run_optimize_best_effort(node)
    db.commit()
    db.refresh(node)
    return node


async def _run_optimize_best_effort(node: Node) -> dict:
    """执行初始优化并兜底：优化失败不阻断添加，异常也收敛为结构化结果。"""
    try:
        return await asyncio.to_thread(node_optimize.optimize_node, node)
    except Exception as e:
        return {
            "ok": False,
            "ran_at": iso_utc(datetime.now(timezone.utc)),
            "steps": [],
            "summary": "初始优化执行异常",
            "warnings": [f"初始优化异常（未影响节点添加）: {e}"],
        }


async def _install_agent_when_creating(node: Node) -> None:
    """（添加节点）部署 Agent 并验证可达；失败抛结构化错误，由调用方回滚节点行。

    - 部署失败（SSH 不可达/参数错误/上传安装失败）→ 422 agent_install_failed；
    - 部署完成但连通性验证失败（agent 端口/防火墙等导致不可达）→ 先卸载刚装的
      Agent 清理远端残留，再 400 agent_verify_failed_rollback 报错回滚。
    """
    result = await deploy_agent.deploy(node)
    if not result.get("ok"):
        err = result.get("error") or "未知错误"
        raise api_error(422, Code.AGENT_INSTALL_FAILED,
                        f"Agent 安装失败（节点已回滚）：{err}",
                        params={"name": node.name, "error": err}, details=err)
    if result.get("hardware_info"):
        node.hardware_info = result["hardware_info"]
        node.agent_status = "online"
        node.last_seen = datetime.now(timezone.utc)
        return
    # 安装完成但连通性验证失败：节点信息不可达，视为失败并尽力清理远端残留
    try:
        await deploy_agent.uninstall(node)
    except Exception:
        pass
    warn = result.get("warning") or "Agent 安装完成但连通性验证失败"
    raise api_error(400, Code.AGENT_VERIFY_FAILED_ROLLBACK,
                    f"Agent 安装完成但无法连通（节点信息不可达），已回滚并清理：{warn}",
                    params={"name": node.name, "error": warn}, details=warn)


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


def _rm_tool_images(node: Node) -> None:
    """SSH 删除节点上本工具管理的 Docker 镜像（模型镜像 + 配方运行时镜像）。"""
    client = ssh_client.connect(node, timeout=20)
    try:
        ssh_client.exec(
            client,
            "docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null "
            "| grep -E 'fireworks-models/|anemll/dspark-vllm-gx10' "
            "| xargs -r docker rmi -f 2>/dev/null; echo IMAGES_CLEANED",
            timeout=300,
        )
    finally:
        client.close()


@router.delete("/{node_id}")
async def delete_node(
    node_id: int,
    cleanup_agent: bool = True,
    cleanup_network: bool = True,
    cleanup_models: bool = False,
    cleanup_images: bool = False,
    db: Session = Depends(get_db),
):
    """删除节点：防御校验后按需卸载 Agent、回滚高速网、清模型/镜像并移除。

    先做防御性校验，拒绝破坏集群/任务拓扑的操作：
    - 节点上存在 active 任务（published/running/paused）→ 拒绝（须先停止/删除任务）；
    - 节点仍属于某个集群 → 拒绝（须先移除成员或删除集群）。
    通过后再按需清理：
    - cleanup_agent: 停止并删除节点上的 Agent（systemd 服务 + 工作目录）；
    - cleanup_network: 回滚节点上的高速网配置并释放占用（集群清空则删除）；
    - cleanup_models / cleanup_images: 删除节点上的模型缓存 / 工具管理的 Docker 镜像。
    各步骤尽力而为，失败进 warnings 不阻断删除。
    """
    node = get_node_or_404(db, node_id)

    # 防御 1：节点上存在 active 任务时拒绝删除。任务容器按节点分布在集群成员上，
    # 直接删除节点会让任务失去该节点却仍在其它成员运行容器（损坏/孤儿任务）。
    active_tasks = (
        db.query(Task)
        .join(TaskNode, TaskNode.task_id == Task.id)
        .filter(TaskNode.node_id == node.id,
                Task.status.in_(("published", "running", "paused")))
        .all()
    )
    if active_tasks:
        names = ", ".join(f"#{t.id} {t.name}（{t.status}）" for t in active_tasks)
        raise api_error(409, Code.NODE_HAS_ACTIVE_TASKS,
                        f"节点上存在未停止的任务（{names}），请先停止/删除这些任务后再删除节点",
                        params={"names": names})

    # 防御 2：节点仍属于集群时拒绝删除（须先移除成员或删除集群），
    # 避免隐式改写集群拓扑造成不可预期的副作用。
    if node.cluster_id:
        cluster = db.get(Cluster, node.cluster_id)
        raise api_error(409, Code.NODE_IN_CLUSTER,
                        f"节点仍属于集群「{cluster.name if cluster else node.cluster_id}」，"
                        "请先从集群移除该成员或删除集群后再删除节点",
                        params={"cluster": cluster.name if cluster else node.cluster_id})

    warnings: list[str] = []

    # 0) 节点上若仍有任务容器（异常残留），尽力停掉避免孤儿（正常流程先删任务）
    projects = set()
    for tn in db.query(TaskNode).filter(TaskNode.node_id == node.id).all():
        task = db.get(Task, tn.task_id)
        if task:
            projects.add(task.name)
    for project in projects:
        try:
            await agent_client.compose_down(node, project)
        except Exception as e:
            warnings.append(f"停止任务容器 {project}: {e}")

    # 1) 所属集群：回滚高速网 + 释放成员占用（空集群则删除）
    cluster = db.get(Cluster, node.cluster_id) if node.cluster_id else None
    if cleanup_network and cluster:
        try:
            ok, msg = network_config_svc.rollback_node_network(node)
            if not ok:
                warnings.append(f"高速网络回滚: {msg}")
        except Exception as e:
            warnings.append(f"高速网络回滚: {e}")
    if cluster:
        db.query(ClusterNode).filter(ClusterNode.node_id == node.id).delete()
        node.cluster_id = None
        db.commit()
        if db.query(ClusterNode).filter(ClusterNode.cluster_id == cluster.id).count() == 0:
            db.delete(cluster)
        db.commit()

    # 2) 可选：经 Agent 删除模型缓存（删除前 agent 仍在跑）
    if cleanup_models and node.agent_token:
        try:
            caches = await agent_client.model_cache(node)
            items = caches if isinstance(caches, list) else (caches or {}).get("items", [])
            for item in items:
                repo = (item or {}).get("repo")
                if not repo:
                    continue
                try:
                    await agent_client.model_delete(node, repo)
                except Exception as e:
                    warnings.append(f"删除模型 {repo}: {e}")
        except Exception as e:
            warnings.append(f"模型清理: {e}")

    # 3) 可选：SSH 删除工具管理的 Docker 镜像
    if cleanup_images:
        try:
            await asyncio.to_thread(_rm_tool_images, node)
        except Exception as e:
            warnings.append(f"镜像清理: {e}")

    # 4) 卸载 Agent（默认执行；未部署则跳过）
    if cleanup_agent:
        if not node.agent_token:
            warnings.append("节点未部署 Agent，跳过卸载")
        else:
            r = await deploy_agent.uninstall(node)
            if not r.get("ok"):
                warnings.append(r.get("error", "Agent 卸载失败"))

    db.delete(node)
    db.commit()
    await agent_ws.broadcast({"type": "node_delete", "node_id": node.id})
    return {"ok": True, "warnings": warnings}


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


@router.post("/{node_id}/optimize")
async def optimize_node(node_id: int, db: Session = Depends(get_db)):
    """手动对节点执行「初始优化」：关闭 Wi-Fi/蓝牙、关闭 GUI、授予 docker 权限、关闭 swap。

    best-effort：无法取得 root 或单项失败不抛错，结果（steps/warnings）落库并返回，
    供前端提示；可对添加时未勾选或本功能上线前已存在的旧节点补跑。
    """
    node = get_node_or_404(db, node_id)
    result = await _run_optimize_best_effort(node)
    # 仅当成功或此前无记录时落库：失败的重跑（如节点临时不可达）不应覆盖既有
    # 「已优化」状态，避免徽标从 已优化 掉回 未完成 造成误导。
    if result.get("ok") or node.optimize_result is None:
        node.optimize_result = result
    db.commit()
    return result


@router.post("/{node_id}/refresh")
async def refresh_node(node_id: int, db: Session = Depends(get_db)):
    """重新拉取 Agent 硬件信息并更新在线状态。"""
    node = get_node_or_404(db, node_id)
    try:
        hw = await agent_client.info(node)
    except Exception as e:
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
    except Exception as e:
        raise api_error(502, Code.NVIDIA_SMI_FAILED, f"nvidia-smi 获取失败: {e}",
                        details=str(e)) from e
    return {"output": output}


@router.get("/{node_id}/containers")
async def node_containers(node_id: int, db: Session = Depends(get_db)):
    node = get_node_or_404(db, node_id)
    try:
        return {"containers": await agent_client.list_containers(node)}
    except Exception as e:
        raise map_agent_error(e) from e


@router.get("/{node_id}/containers/{name}/logs")
async def node_container_logs(node_id: int, name: str, tail: int = 200, db: Session = Depends(get_db)):
    node = get_node_or_404(db, node_id)
    try:
        logs = await agent_client.container_logs(node, name, tail)
    except Exception as e:
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
    except Exception as e:
        raise map_agent_error(e) from e


@router.get("/{node_id}/models")
async def node_models(node_id: int, db: Session = Depends(get_db)):
    """节点上已接收的模型列表（模型管理）。"""
    node = get_node_or_404(db, node_id)
    try:
        return await agent_client.model_cache(node)
    except Exception as e:
        raise map_agent_error(e) from e


@router.delete("/{node_id}/models/{repo:path}")
async def node_model_delete(node_id: int, repo: str, db: Session = Depends(get_db)):
    """删除节点上的指定模型。"""
    node = get_node_or_404(db, node_id)
    try:
        return await agent_client.model_delete(node, repo)
    except Exception as e:
        raise map_agent_error(e) from e
