"""任务管理：发布（worker-first 编排）、暂停/继续/停止/删除、日志、健康检查。"""

import asyncio
import copy
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from .. import config, schemas
from ..background_tasks import spawn
from ..db import SessionLocal, get_db
from ..errors import Code, api_error
from ..models import (
    Cluster,
    ClusterNode,
    InferenceSample,
    Node,
    Recipe,
    Task,
    TaskBenchmark,
    TaskNode,
    iso_utc,
)
from ..services import (
    agent_client,
    agent_ws,
    llm_stats,
    node_info,
    recipe_render,
    task_runtime,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def get_task_or_404(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise api_error(404, Code.TASK_NOT_FOUND, "任务不存在")
    return task


def task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "name": task.name,
        "recipe_id": task.recipe_id,
        "cluster_id": task.cluster_id,
        "status": task.status,
        "variables": task.variables,
        "rendered": task.rendered,
        "error": task.error,
        "created_at": iso_utc(task.created_at),
        "updated_at": iso_utc(task.updated_at),
        "nodes": [
            {
                "id": tn.id,
                "node_id": tn.node_id,
                "role": tn.role,
                "node_rank": tn.node_rank,
                "container_name": tn.container_name,
                "container_status": tn.container_status,
                "error": tn.error,
            }
            for tn in task.nodes
        ],
    }


@router.get("")
def list_tasks(db: Session = Depends(get_db)):
    tasks = db.query(Task).order_by(Task.id.desc()).all()
    return [task_to_dict(t) for t in tasks]


@router.get("/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db)):
    return task_to_dict(get_task_or_404(db, task_id))


@router.post("", status_code=201)
async def create_task(req: schemas.TaskCreate, db: Session = Depends(get_db)):
    """发布任务：渲染配方 -> 逐节点 compose（worker 先起、head 后起）-> 后台健康检查。"""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", req.name):
        raise api_error(400, Code.TASK_NAME_INVALID, "任务名只能包含字母、数字及 . _ -")
    if db.query(Task).filter(Task.name == req.name).first():
        raise api_error(409, Code.TASK_ALREADY_EXISTS, "同名任务已存在")

    recipe = db.get(Recipe, req.recipe_id)
    if not recipe:
        raise api_error(404, Code.RECIPE_NOT_FOUND, "配方不存在")
    cluster = db.get(Cluster, req.cluster_id)
    if not cluster:
        raise api_error(404, Code.CLUSTER_NOT_FOUND, "集群不存在")

    member_map = {m.node_id: m for m in cluster.members}

    # 任务级 head/worker/rank 分配：发布时显式指定每个节点的角色与 rank（随任务保存），
    # 与集群成员解耦——同一集群可发布多个任务，各自有不同的 head/worker/rank。
    if not req.nodes:
        raise api_error(400, Code.TASK_NO_HEAD, "请至少指定一个节点（head 必选，可按需加 worker）")
    node_ids = [a.node_id for a in req.nodes]
    if len(node_ids) != len(set(node_ids)):
        raise api_error(400, Code.TASK_NODE_DUPLICATED,
                        "同一节点不能在一个任务中重复分配",
                        params={"node_ids": node_ids})
    heads = [a for a in req.nodes if a.role == "head"]
    if len(heads) != 1:
        raise api_error(400, Code.TASK_NO_HEAD, "必须且只能指定一个 head 节点")
    head = db.get(Node, heads[0].node_id)
    if not head or head.id not in member_map:
        raise api_error(400, Code.HEAD_NOT_IN_CLUSTER, "Head 节点必须在所选集群中")
    # 分布式初始化要求 MASTER_ADDR（= head 的 RoCE IP）指向 rank0 节点
    if heads[0].node_rank != 0:
        raise api_error(400, Code.TASK_HEAD_NOT_RANK0,
                        "分布式协调要求 MASTER_ADDR 即 rank0（head），head 节点 rank 必须为 0")

    assignments = []
    rank_of: dict[int, int] = {}
    for a in req.nodes:
        node = db.get(Node, a.node_id)
        if not node or node.id not in member_map:
            raise api_error(400, Code.WORKER_NOT_IN_CLUSTER,
                            f"节点 {a.node_id} 不在所选集群中", params={"id": a.node_id})
        if a.node_rank in rank_of:
            raise api_error(400, Code.TASK_RANK_TAKEN,
                            f"node_rank {a.node_rank} 已被节点 {rank_of[a.node_rank]} 占用",
                            params={"rank": a.node_rank})
        rank_of[a.node_rank] = node.id
        assignments.append((node, a.role, a.node_rank))

    # 固定拓扑校验：配方声明了确切节点数（node_count，参考 vLLM recipes 按固定数量
    # 设备调优）时，任务节点数必须恰好匹配——不做 min/max 比较。
    if recipe.node_count and len(assignments) != recipe.node_count:
        raise api_error(
            400, Code.TASK_NODE_COUNT_MISMATCH,
            f"该配方针对 {recipe.node_count} 台节点调优（固定拓扑），"
            f"发布任务必须恰好选择 {recipe.node_count} 台节点（当前 {len(assignments)} 台）",
            params={"required": recipe.node_count, "selected": len(assignments)},
        )

    all_nodes = [n for n, _, _ in assignments]

    # 节点互斥：同一节点不能同时承载多个 active 任务（端口/GPU/RoCE 冲突）。
    # 一个集群发布多个任务时，请使用不重叠的节点子集，各自指定 head/worker/rank。
    occupied = (
        db.query(TaskNode.node_id)
        .join(Task, Task.id == TaskNode.task_id)
        .filter(Task.status.in_(["published", "running", "paused"]),
                TaskNode.node_id.in_({n.id for n in all_nodes}))
        .all()
    )
    if occupied:
        raise api_error(409, Code.NODE_BUSY,
                        "所选节点正在被其他任务使用（同一节点不能同时运行多个任务），"
                        "请用不重叠的节点子集发布不同任务")

    # 渲染变量可能依赖 GPU / HCA / GID / 磁盘等节点信息。发布是最终一致性边界：
    # 必须向所有所选 Agent 读取当前信息，不能使用添加节点时留下的旧快照。
    try:
        await node_info.refresh_nodes(db, all_nodes)
    except node_info.NodeInfoRefreshError as e:
        raise api_error(
            502, Code.AGENT_UNREACHABLE,
            f"无法获取节点最新信息，任务未发布：{e}", details=str(e),
        ) from e

    # 刷新节点会提交事务并让 ORM 快照失效；期间配方/集群可能被其他请求删除或修改。
    # 渲染前重新取得上下文，避免使用已删除对象或过期的成员拓扑。
    recipe = db.get(Recipe, req.recipe_id, populate_existing=True)
    cluster = db.get(Cluster, req.cluster_id, populate_existing=True)
    if not recipe or not cluster:
        raise api_error(
            409,
            Code.TASK_STATE_CHANGED,
            "节点信息刷新期间配方或集群已被删除，请重新选择后发布",
        )
    selected_node_ids = {node.id for node in all_nodes}
    member_map = {m.node_id: m for m in cluster.members}
    if not selected_node_ids.issubset(member_map):
        raise api_error(
            409,
            Code.TASK_STATE_CHANGED,
            "节点信息刷新期间集群成员已变化，请刷新后重试",
        )
    if recipe.node_count and len(assignments) != recipe.node_count:
        raise api_error(
            409,
            Code.TASK_STATE_CHANGED,
            "节点信息刷新期间配方拓扑已变化，请重新预览并发布",
        )

    try:
        rendered = recipe_render.render_task(
            recipe, cluster, assignments, req.variables, req.name
        )
    except recipe_render.RenderError as e:
        raise HTTPException(422, str(e)) from e

    # 模型/镜像保障仍可能持续较长时间。保存渲染所依据的纯值快照，取得写锁后
    # 再核对一次，防止提交引用已删除或与渲染结果不一致的配方/集群配置。
    recipe_revision = recipe.updated_at
    cluster_snapshot = (
        cluster.network_type,
        cluster.network_cidr,
        cluster.network_mtu,
        copy.deepcopy(cluster.network_plan),
        {node_id: member_map[node_id].net_index for node_id in selected_node_ids},
    )

    # 模型保障（与任务解耦，可按需关闭）：配方含 DSPARK_MODEL 且 send_model 时，
    # 缺失则走管理传输（控制平面下载 -> 管理网发送 head -> Agent 高速直传 worker）；
    # 全部就绪后强制离线发布，避免各节点同时从互联网下载抢占带宽
    head_env = rendered["nodes"][str(head.id)]["env"]
    model_repo = head_env.get("DSPARK_MODEL")
    if model_repo and req.send_model:
        from ..services import model_manager

        try:
            ensure = await model_manager.ensure_model_on_nodes(
                model_repo, "main", all_nodes, head.id
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        if not ensure["ok"]:
            raise HTTPException(
                409,
                ensure["message"]
                + "；模型就绪后请重新发布（发布会话使用本地缓存，不再联网下载）",
            )
        # 全部节点已就绪 -> 强制离线，避免重复下载
        for payload in rendered["nodes"].values():
            payload["env"]["HF_HUB_OFFLINE"] = "true"

    # 镜像保障（与任务解耦，可按需关闭）：配方含镜像快速选择变量且 send_image 时，
    # 缺失则走管理传输（控制平面归档 -> head -> RoCE 同步 -> 各节点 docker load）
    image_var = next(
        (v for v in (recipe.variables or []) if v.get("picker") == "image"), None
    )
    image_repo = head_env.get(image_var["key"]) if image_var else None
    if image_repo and req.send_image:
        from ..services import image_manager

        try:
            ensure_img = await image_manager.ensure_image_on_nodes(
                image_repo, all_nodes, head.id
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        if not ensure_img["ok"]:
            raise HTTPException(
                409,
                ensure_img["message"]
                + "；镜像就绪后请重新发布（发布会话使用本地归档，不再联网拉取）",
            )

    # Agent 刷新是长 I/O；刷新期间其它请求可能抢占相同节点。这里先取得数据库
    # 写锁再复查占用，并在同一事务中创建 task/task_nodes，形成原子节点预留。
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        from sqlalchemy import text

        db.execute(text("BEGIN IMMEDIATE"))
    else:
        db.query(Node).filter(Node.id.in_({n.id for n in all_nodes})).with_for_update().all()
        db.query(Recipe).filter(Recipe.id == req.recipe_id).with_for_update().first()
        db.query(Cluster).filter(Cluster.id == req.cluster_id).with_for_update().first()
        (
            db.query(ClusterNode)
            .filter(ClusterNode.cluster_id == req.cluster_id)
            .with_for_update()
            .all()
        )

    fresh_recipe = db.get(Recipe, req.recipe_id, populate_existing=True)
    fresh_cluster = db.get(Cluster, req.cluster_id, populate_existing=True)
    if not fresh_recipe or not fresh_cluster:
        db.rollback()
        raise api_error(
            409,
            Code.TASK_STATE_CHANGED,
            "模型或镜像准备期间配方或集群已被删除，请重新选择后发布",
        )
    fresh_member_indices = dict(
        db.query(ClusterNode.node_id, ClusterNode.net_index)
        .filter(
            ClusterNode.cluster_id == req.cluster_id,
            ClusterNode.node_id.in_(selected_node_ids),
        )
        .all()
    )
    fresh_cluster_snapshot = (
        fresh_cluster.network_type,
        fresh_cluster.network_cidr,
        fresh_cluster.network_mtu,
        fresh_cluster.network_plan,
        fresh_member_indices,
    )
    if fresh_recipe.updated_at != recipe_revision or fresh_cluster_snapshot != cluster_snapshot:
        db.rollback()
        raise api_error(
            409,
            Code.TASK_STATE_CHANGED,
            "模型或镜像准备期间配方或集群配置已变化，请重新预览并发布",
        )
    occupied = (
        db.query(TaskNode.node_id)
        .join(Task, Task.id == TaskNode.task_id)
        .filter(
            Task.status.in_(["published", "running", "paused"]),
            TaskNode.node_id.in_({n.id for n in all_nodes}),
        )
        .first()
    )
    if occupied:
        db.rollback()
        raise api_error(
            409,
            Code.NODE_BUSY,
            "节点信息刷新期间，所选节点已被其他任务占用，请刷新后重试",
        )
    if db.query(Task).filter(Task.name == req.name).first():
        db.rollback()
        raise api_error(409, Code.TASK_ALREADY_EXISTS, "同名任务已存在")

    task = Task(
        name=req.name,
        recipe_id=recipe.id,
        cluster_id=cluster.id,
        status="published",
        variables=req.variables,
        rendered=rendered,
    )
    db.add(task)
    db.flush()
    for node, role, rank in assignments:
        db.add(TaskNode(task_id=task.id, node_id=node.id, role=role, node_rank=rank))
    db.commit()

    # worker-first 启动顺序（参考配方经验，避免 mp-init 竞态）
    ordered = sorted(assignments, key=lambda a: (a[1] == "head", a[2]))
    errors = []
    started = []  # 已成功 compose up 的节点，任一杯子失败时回滚清理
    for node, role, rank in ordered:
        payload = rendered["nodes"][str(node.id)]
        tn = (
            db.query(TaskNode).filter_by(task_id=task.id, node_id=node.id).first()
        )
        try:
            await agent_client.compose_up(
                node, payload["project"], payload["compose_yaml"], payload["env"]
            )
            started.append(node)  # compose 已拉起（容器可能已启动），失败时需回滚
            ps = await agent_client.compose_ps(node, payload["project"])
            containers = ps.get("containers", [])
            if containers:
                tn.container_name = containers[0].get("name")
                tn.container_status = containers[0].get("state")
            db.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{node.name}: {e}")
            tn.error = str(e)
            db.commit()

    if errors:
        # 部分节点已启动：逐个 compose down 回滚，避免 GPU 容器残留泄漏资源
        for node in started:
            payload = rendered["nodes"][str(node.id)]
            try:
                await agent_client.compose_down(node, payload["project"])
                tn2 = (
                    db.query(TaskNode).filter_by(task_id=task.id, node_id=node.id).first()
                )
                if tn2:
                    tn2.container_status = "exited"
                db.commit()
            except Exception as e:  # noqa: BLE001
                errors.append(f"回滚清理 {node.name}: {e}")
        task.status = "error"
        task.error = "; ".join(errors)
        db.commit()
        await agent_ws.broadcast({"type": "task_status", "task_id": task.id,
                                  "status": "error"})
        return task_to_dict(task)

    # 容器已全部拉起 -> running；仅当配方含 VLLM_PORT（vLLM 类服务）才做健康检查
    task.status = "running"
    db.commit()
    await agent_ws.broadcast({"type": "task_status", "task_id": task.id,
                              "status": "running"})
    head_env = rendered["nodes"][str(head.id)]["env"]
    vllm_port = head_env.get("VLLM_PORT")
    if vllm_port:
        spawn(_health_check(task.id, head.id, vllm_port))
    return task_to_dict(task)


async def _health_check(task_id: int, head_node_id: int, vllm_port: str) -> None:
    """发布后轮询 head 节点 vLLM /v1/models 直到就绪或超时。

    每次写状态前复查任务当前 DB 状态：用户 pause/stop/删除后（或 task_monitor
    置 stopped）不得覆盖用户操作，直接退出。
    """
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or task.status not in ("published", "running"):
            return
        head = db.get(Node, head_node_id)
        url = f"http://127.0.0.1:{vllm_port}/v1/models"
        deadline = time.time() + config.TASK_HEALTH_TIMEOUT
        while time.time() < deadline:
            try:
                resp = await agent_client.http_get(head, url, timeout=10)
                if resp.get("status") == 200:
                    if not _still_manageable(db, task, task_id):
                        return
                    task.status = "running"
                    db.commit()
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(config.TASK_HEALTH_INTERVAL)
        if not _still_manageable(db, task, task_id):
            return
        task.status = "error"
        task.error = f"健康检查超时：节点 {head.name} 的 {url} 未在 {config.TASK_HEALTH_TIMEOUT}s 内就绪"
        db.commit()
    finally:
        db.close()


def _still_manageable(db: Session, task: Task, task_id: int) -> bool:
    """复查任务当前 DB 状态是否仍可被健康检查推进（避免覆盖用户操作）。

    任务已被删除时 refresh 抛 ObjectDeletedError -> False；状态被改为
    paused/stopped/error 时同样返回 False。
    """
    try:
        db.refresh(task)
    except Exception:  # noqa: BLE001 - 任务已被删除
        return False
    return task.status in ("published", "running")


def schedule_health_checks() -> int:
    """对存量 running/published 任务补发健康检查（后端重启后恢复）。"""
    db = SessionLocal()
    count = 0
    try:
        tasks = db.query(Task).filter(Task.status.in_(["published", "running"])).all()
        for task in tasks:
            rendered = task.rendered or {}
            nodes = rendered.get("nodes") or {}
            head_entry = None
            for node_id, payload in nodes.items():
                if payload.get("role") == "head":
                    head_entry = (int(node_id), payload)
                    break
            if not head_entry:
                continue
            head_node_id, payload = head_entry
            vllm_port = payload.get("env", {}).get("VLLM_PORT")
            if vllm_port:
                spawn(_health_check(task.id, head_node_id, vllm_port))
                count += 1
        return count
    finally:
        db.close()


@router.post("/{task_id}/action")
async def task_action(task_id: int, req: schemas.TaskActionRequest, db: Session = Depends(get_db)):
    task = get_task_or_404(db, task_id)
    action = req.action
    errors = []

    if action == "pause":
        for tn in task.nodes:
            if not tn.container_name:
                continue
            node = db.get(Node, tn.node_id)
            try:
                await agent_client.container_action(node, tn.container_name, "pause")
                tn.container_status = "paused"
            except Exception as e:  # noqa: BLE001
                errors.append(f"{tn.node_id}: {e}")
        task.status = "paused"
    elif action == "resume":
        for tn in task.nodes:
            if not tn.container_name:
                continue
            node = db.get(Node, tn.node_id)
            try:
                await agent_client.container_action(node, tn.container_name, "unpause")
                tn.container_status = "running"
            except Exception as e:  # noqa: BLE001
                errors.append(f"{tn.node_id}: {e}")
        task.status = "running"
    elif action in ("stop", "delete"):
        for tn in task.nodes:
            node = db.get(Node, tn.node_id)
            try:
                await agent_client.compose_down(node, task.name)
                tn.container_status = "exited"
            except Exception as e:  # noqa: BLE001
                errors.append(f"{tn.node_id}: {e}")
        # 模型与任务解耦：可选在终止时删除节点上的模型（释放磁盘）
        head_repo = None
        if req.delete_model:
            rendered_nodes = ((task.rendered or {}).get("nodes") or {})
            for payload in rendered_nodes.values():
                if payload.get("role") == "head":
                    head_repo = payload.get("env", {}).get("DSPARK_MODEL")
                    break
            if head_repo:
                for tn in task.nodes:
                    node = db.get(Node, tn.node_id)
                    try:
                        await agent_client.model_delete(node, head_repo)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"删除模型 {tn.node_id}: {e}")
        if action == "stop":
            task.status = "stopped"
        else:
            try:
                # 与推理统计/压测的晚到写入使用同一数据库锁，确保清理完成后不会再
                # 插入该任务的运行时记录。
                locked_task = task_runtime.lock_task_for_write(db, task.id)
                if locked_task is None:
                    raise api_error(
                        409, Code.TASK_STATE_CHANGED,
                        "任务已被删除或状态已变更，请刷新后重试",
                    )
                task = locked_task
                # SQLite 未启用外键级联；显式清理任务域数据，避免孤儿记录在
                # 新任务获得相同 id 时被误认为新任务的历史数据。
                db.query(InferenceSample).filter(
                    InferenceSample.task_id == task.id
                ).delete(synchronize_session=False)
                db.query(TaskBenchmark).filter(
                    TaskBenchmark.task_id == task.id
                ).delete(synchronize_session=False)
                db.delete(task)
                db.commit()
            except StaleDataError:
                # 并发/重复操作：任务已被其他请求删除
                raise api_error(409, Code.TASK_STATE_CHANGED,
                                "任务已被删除或状态已变更，请刷新后重试") from None
            await agent_ws.broadcast({"type": "task_deleted", "task_id": task.id})
            return {"ok": True, "errors": errors, "model_deleted": head_repo if req.delete_model else False}
    else:
        raise HTTPException(400, f"未知动作: {action}")

    if errors:
        task.error = "; ".join(errors)
    try:
        db.commit()
    except StaleDataError:
        # 并发/重复操作（如停止时任务已被删除）：不覆盖用户操作，返回明确错误
        raise api_error(409, Code.TASK_STATE_CHANGED,
                        "任务已被删除或状态已变更，请刷新后重试") from None
    db.refresh(task)
    # 实时广播：详情页/列表页/总览页状态即时更新（无需刷新）
    await agent_ws.broadcast({"type": "task_status", "task_id": task.id,
                              "status": task.status})
    return task_to_dict(task)


@router.get("/{task_id}/logs")
async def task_logs(task_id: int, node_id: int, tail: int = 200, db: Session = Depends(get_db)):
    get_task_or_404(db, task_id)  # 404 检查
    tn = db.query(TaskNode).filter_by(task_id=task_id, node_id=node_id).first()
    if not tn or not tn.container_name:
        raise api_error(404, Code.CONTAINER_NOT_FOUND, "该节点上无此任务的容器")
    node = db.get(Node, node_id)
    try:
        logs = await agent_client.container_logs(node, tn.container_name, tail)
    except Exception as e:  # noqa: BLE001 - 容器已停止/删除时 agent 返回 404
        raise api_error(404, Code.CONTAINER_LOG_UNAVAILABLE,
                        f"容器日志不可用（容器可能已停止或已删除）：{e}",
                        details=str(e)) from e
    return {
        "node_id": node_id,
        "node_name": node.name if node else None,
        "container": tn.container_name,
        "logs": logs,
    }


# 每个任务最多保留的基准测试结果条数（卡片展示最近几次）
BENCHMARK_KEEP = 5


@router.get("/{task_id}/benchmarks")
def task_benchmarks(task_id: int, limit: int = 5, db: Session = Depends(get_db)):
    """该任务的基准测试历史（最新在前），供详情页卡片回顾。"""
    get_task_or_404(db, task_id)
    rows = (
        db.query(TaskBenchmark)
        .filter(TaskBenchmark.task_id == task_id)
        .order_by(TaskBenchmark.ts.desc())
        .limit(limit)
        .all()
    )
    return [{"ts": r.ts, "result": r.result} for r in rows]


@router.post("/{task_id}/benchmark")
async def run_task_benchmark(
    task_id: int, req: schemas.BenchmarkRequest, db: Session = Depends(get_db)
):
    """对运行中推理任务执行并发 decode 压测，结果持久化（保留 BENCHMARK_KEEP 条）。"""
    task = get_task_or_404(db, task_id)
    if task.status != "running":
        raise api_error(400, Code.TASK_STATE_CHANGED,
                        "仅运行中的任务可执行基准测试")
    endpoint = llm_stats.service_endpoint(db, task)
    if endpoint is None:
        raise api_error(400, Code.TASK_STATE_CHANGED,
                        "该任务无推理端点（head 无 VLLM_PORT），不支持基准测试")
    head, url_base, model = endpoint
    if not agent_ws.is_connected(head.id):
        raise api_error(502, Code.AGENT_UNREACHABLE,
                        "Head 节点当前离线，无法执行基准测试")
    try:
        result = await agent_client.llm_benchmark(head, {
            "url_base": url_base,
            "model": model or "default",
            "concurrency": req.concurrency,
            "num_requests": req.num_requests,
            "max_tokens": req.max_tokens,
            "timeout": 120,
        })
    except Exception as e:  # noqa: BLE001
        raise agent_client.map_agent_error(e) from e
    # 压测期间任务可能被停止或删除；锁定并重新读取后才允许保存结果。
    task = task_runtime.lock_task_for_write(db, task.id, {"running"})
    if task is None:
        db.rollback()
        raise api_error(409, Code.TASK_STATE_CHANGED, "压测完成时任务已停止或删除，结果未保存")
    # 持久化 + 裁剪（并发/重复运行下保留最近 N 条）
    db.add(TaskBenchmark(task_id=task.id, ts=time.time(), result=result))
    count = db.query(TaskBenchmark).filter(TaskBenchmark.task_id == task.id).count()
    if count > BENCHMARK_KEEP:
        stale = (
            db.query(TaskBenchmark)
            .filter(TaskBenchmark.task_id == task.id)
            .order_by(TaskBenchmark.ts.asc())
            .limit(count - BENCHMARK_KEEP)
            .all()
        )
        for s in stale:
            db.delete(s)
    db.commit()
    await agent_ws.broadcast({"type": "benchmark_result", "task_id": task.id,
                              "url_base": url_base, "result": result})
    return {"task_id": task.id, "url_base": url_base, "result": result}
