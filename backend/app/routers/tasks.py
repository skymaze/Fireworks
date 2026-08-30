"""任务管理：发布（worker-first 编排）、暂停/继续/停止/删除、日志、健康检查。"""

import asyncio
import copy
import re
import threading
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
    task_monitor,
    task_runtime,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# 每任务状态转移锁（单 uvicorn worker 下有效）：并发 pause/resume/stop/delete
# 在同一任务上串行化，配合部署循环的状态复查，避免「双击 stop」「stop+resume」
# 交错覆盖 DB 状态而脱离真实容器状态。任务主键 AUTOINCREMENT 不复用，锁不会串到新任务。
_task_action_locks: dict[int, asyncio.Lock] = {}
_task_action_locks_guard = threading.Lock()


def _task_action_lock(task_id: int) -> asyncio.Lock:
    with _task_action_locks_guard:
        return _task_action_locks.setdefault(task_id, asyncio.Lock())


def _release_task_action_lock(task_id: int) -> None:
    with _task_action_locks_guard:
        _task_action_locks.pop(task_id, None)


def _validate_transition(current: str, action: str) -> None:
    """任务状态转移合法性：阻止把已停止/已删除的任务误置为 running/paused。

    此前 resume 对 stopped/error 任务也会置 running，但容器已被 compose_down，
    导致任务永久停留在「无容器的 running」，监控无法自愈。
    """
    allowed = {
        "pause": {"published", "running"},
        "resume": {"paused"},
        "stop": {"published", "running", "paused", "error"},
        "restart": {"running"},          # 运行中重启（docker compose restart，不重建）
        "start": {"stopped", "error"},   # 停止后启动（docker compose start，复用容器）
        # delete 允许删除任意存在状态（含 stopped/error）
    }
    if action in allowed and current not in allowed[action]:
        raise api_error(
            409, Code.TASK_STATE_CHANGED,
            f"任务当前状态 {current} 不允许操作 {action}，请刷新后重试",
        )


# 任务名 = docker compose 项目名（recipe_render 把它原样设为 project，agent 以
# `docker compose -p <project>` 启动）。节点 Docker Compose v5 强制项目名只含
# 小写字母/数字/'-'/'_'，且以字母或数字开头——**不能含点（`.`）或大写**。
# 在此先拒绝，避免发布到节点后才因项目名非法而失败（agent compose up rc!=0
# -> 502，表现为 httpx "Server error '502 Bad Gateway'"）。
_TASK_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _validate_task_name(name: str) -> str:
    if not _TASK_NAME_RE.fullmatch(name or ""):
        raise api_error(
            400, Code.TASK_NAME_INVALID,
            "任务名只允许小写字母、数字及 - _（不能含点、大写或空格）："
            "任务名即 docker compose 项目名，节点 Docker Compose v5 有此硬性限制，"
            "示例：glm53-flash-nv",
        )
    return name


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
        # 任务层面健康（head 容器 compose healthcheck 聚合）：healthy|unhealthy|
        # starting|""(未配置)；节点层面只有容器状态 container_status
        "health": task.health,
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
    _validate_task_name(req.name)
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

    # 模型保障（与任务解耦，可按需关闭）：配方可声明多个模型变量（picker=="model"，
    # 如主模型 + DSPARK 草稿模型），逐个保障就绪；缺失走管理传输（控制平面下载 ->
    # 管理网发送 head -> Agent 高速直传 worker），全部就绪后强制离线发布。
    # 键名随配方而异，按 picker=="model" 动态取键；同仓库重复引用只保障一次。
    head_env = rendered["nodes"][str(head.id)]["env"]
    model_repos = [
        head_env[v["key"]]
        for v in (recipe.variables or [])
        if v.get("picker") == "model" and head_env.get(v["key"])
    ]
    model_repos = list(dict.fromkeys(model_repos))
    if model_repos:
        # MODEL_ID 记主模型（推理统计等下游取用），MODEL_IDS 记全部（终止时逐个删）
        for payload in rendered["nodes"].values():
            payload["env"]["MODEL_ID"] = model_repos[0]
            payload["env"]["MODEL_IDS"] = ",".join(model_repos)
    if model_repos and req.send_model:
        from ..services import model_manager

        for model_repo in model_repos:
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

    # 镜像保障（与任务解耦，可按需关闭）：配方可声明多个镜像变量（picker=="image"），
    # 缺失则走管理传输（控制平面归档 -> head -> RoCE 同步 -> 各节点 docker load）
    image_repos = [
        head_env[v["key"]]
        for v in (recipe.variables or [])
        if v.get("picker") == "image" and head_env.get(v["key"])
    ]
    image_repos = list(dict.fromkeys(image_repos))
    if image_repos and req.send_image:
        from ..services import image_manager

        for image_repo in image_repos:
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
        # 复查：部署期间用户可能已 stop/pause/delete——此时不再启动后续节点、
        # 不覆盖用户状态（stop/pause 的容器清理由 task_action 完成）。
        db.refresh(task)
        if task.status != "published":
            return task_to_dict(task)
        payload = rendered["nodes"][str(node.id)]
        tn = (
            db.query(TaskNode).filter_by(task_id=task.id, node_id=node.id).first()
        )
        try:
            await agent_client.compose_up(
                node, payload["project"], payload["compose_yaml"], payload["env"]
            )
            started.append(node)  # compose 已拉起（容器可能已启动），失败时需回滚
            # 复查：用户可能恰在本次 compose_up 期间介入（stop/pause/delete）——
            # 此时立即回滚刚启动的节点，避免「stopped 任务残留运行容器」的泄漏窗口。
            db.refresh(task)
            if task.status != "published":
                try:
                    await agent_client.compose_down(node, payload["project"])
                except Exception as e:
                    errors.append(f"回滚清理 {node.name}: {e}")
                return task_to_dict(task)
            ps = await agent_client.compose_ps(node, payload["project"])
            containers = ps.get("containers", [])
            if containers:
                tn.container_name = containers[0].get("name")
                tn.container_status = containers[0].get("state")
            db.commit()
        except Exception as e:
            # 用 map_agent_error 提取 agent 返回的 body（如 `docker compose up 失败:
            # invalid project name ...`），而不是暴露 httpx 原始
            # "Server error '502 Bad Gateway'"——便于用户直接看到真实原因。
            msg = agent_client.map_agent_error(e).detail.get("msg", str(e))
            errors.append(f"{node.name}: {msg}")
            tn.error = msg
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
            except Exception as e:
                errors.append(f"回滚清理 {node.name}: {e}")
        task.status = "error"
        task.error = "; ".join(errors)
        db.commit()
        await agent_ws.broadcast({"type": "task_status", "task_id": task.id,
                                  "status": "error"})
        return task_to_dict(task)

    # 容器已全部拉起 -> running；健康检查（compose healthcheck 或按配方降级）随后补发。
    # 结束前再次复查：用户部署期间的 stop/pause 不应被覆盖。
    db.refresh(task)
    if task.status != "published":
        return task_to_dict(task)
    task.status = "running"
    db.commit()
    await agent_ws.broadcast({"type": "task_status", "task_id": task.id,
                              "status": "running"})
    spawn(_health_check(task.id))
    return task_to_dict(task)


def _health_signals_up(signals: list[dict]) -> bool:
    """head 容器是否已拉起（读到真实健康信号）。

    采集失败/容器缺失时 health 为 None（不臆断）；""表示容器在但未声明
    healthcheck。只要取到真实信号即视为容器已拉起。
    """
    return any(s.get("health") is not None for s in signals)


async def _health_check(task_id: int) -> None:
    """发布/启动/重启后按 head 容器健康**补读一次**（docker compose healthcheck）。

    健康判定完全交给 Docker 自身 healthcheck，本函数只按需**读取**并同步
    Task.health 快照（healthy/unhealthy/starting/未配置），不设"超时判死"、
    不把健康转成任务 error（Portainer 式：unhealthy 只是徽标，恢复由 Docker
    自身 healthcheck 完成）。仍在 starting（模型加载期）时按 TASK_HEALTH_INTERVAL
    采样，采样窗（TASK_HEALTH_STEADY_WINDOW）结束仍 starting 则交还
    task_monitor 的 30s 循环持续刷新，任务状态不受影响。容器已拉起时把
    残留在 published 的任务推进为 running（生命周期只由容器状态决定）。
    每次写状态前复查任务当前 DB 状态：用户 pause/stop/删除后（或 task_monitor
    置 stopped）不得覆盖用户操作，直接退出。
    """
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or task.status not in ("published", "running"):
            return
        deadline = time.time() + config.TASK_HEALTH_STEADY_WINDOW
        while time.time() < deadline:
            signals = await task_monitor.collect_container_health(db, task)
            sig = task_monitor.aggregate_task_health(signals)
            if (
                task.status == "published"
                and _health_signals_up(signals)
                and _still_manageable(db, task, task_id)
            ):
                task.status = "running"
                db.commit()
            if sig == "starting":
                # 启动期（含采集失败视为不可判定）：只同步健康快照，继续采样
                if _still_manageable(db, task, task_id):
                    task.health = "starting"
                    db.commit()
                else:
                    return
                await asyncio.sleep(config.TASK_HEALTH_INTERVAL)
                continue
            # healthy / unhealthy / 未配置：读到 Docker 定态即收尾，交给监控持续刷新
            if _still_manageable(db, task, task_id):
                task.health = "" if sig == "no-check" else sig
                db.commit()
            return
    finally:
        db.close()


def _still_manageable(db: Session, task: Task, task_id: int) -> bool:
    """复查任务当前 DB 状态是否仍可被健康检查推进（避免覆盖用户操作）。

    任务已被删除时 refresh 抛 ObjectDeletedError -> False；状态被改为
    paused/stopped/error 时同样返回 False。
    """
    try:
        db.refresh(task)
    except Exception:
        return False
    return task.status in ("published", "running")


def _task_project(task: Task, tn: TaskNode) -> str:
    """节点上 compose 项目名：优先 rendered 快照，回退任务名（与任务监控一致）。"""
    return (
        ((task.rendered or {}).get("nodes") or {}).get(str(tn.node_id), {})
        .get("project") or task.name
    )


def _task_node_payload(task: Task, node_id: int) -> dict | None:
    """节点首次发布的 rendered 配置（compose_yaml/env 等）；缺失返回 None。"""
    return ((task.rendered or {}).get("nodes") or {}).get(str(node_id)) or None


def _task_has_containers(task: Task) -> bool:
    """任务任一节点已记录容器名（存在可复用容器的前提）。"""
    return any(bool(tn.container_name) for tn in task.nodes)


def _schedule_task_health_check(db: Session, task: Task) -> None:
    """start/restart 拉起容器后补发健康检查（compose healthcheck 或按配方降级）。"""
    for tn in task.nodes:
        if tn.role == "head":
            spawn(_health_check(task.id))
            return


def schedule_health_checks() -> int:
    """对存量 published 任务补发健康检查（后端重启后恢复）。"""
    db = SessionLocal()
    count = 0
    try:
        tasks = db.query(Task).filter(Task.status == "published").all()
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
            spawn(_health_check(task.id))
            count += 1
        return count
    finally:
        db.close()


@router.post("/{task_id}/action")
async def task_action(task_id: int, req: schemas.TaskActionRequest, db: Session = Depends(get_db)):
    """任务操作（pause/resume/stop/start/restart/delete）。

    串行化同一任务的并发操作并校验状态转移：非法转移（如对已停止任务 resume）
    返回 409；容器操作失败时不再虚报成功状态（置 error，保留真实容器状态）。

    生命周期语义（均不重建容器）：
    - stop    : docker compose stop（项目级停止，保留容器供 start 复用）
    - start   : docker compose start（复用已停止容器；容器已被清理时回退 compose up 重建）
    - restart : docker compose restart（运行中进程级重启）
    - delete  : docker compose down（彻底移除容器与网络）
    """
    async with _task_action_lock(task_id):
        task = get_task_or_404(db, task_id)
        action = req.action
        _validate_transition(task.status, action)
        errors = []

        if action == "pause":
            for tn in task.nodes:
                if not tn.container_name:
                    continue
                node = db.get(Node, tn.node_id)
                try:
                    await agent_client.container_action(node, tn.container_name, "pause")
                    tn.container_status = "paused"
                except Exception as e:
                    errors.append(f"{tn.node_id}: {e}")
        elif action == "resume":
            for tn in task.nodes:
                if not tn.container_name:
                    continue
                node = db.get(Node, tn.node_id)
                try:
                    await agent_client.container_action(node, tn.container_name, "unpause")
                    tn.container_status = "running"
                except Exception as e:
                    errors.append(f"{tn.node_id}: {e}")
        elif action == "stop":
            # 停止：docker compose stop（项目级停止，保留容器供 start 复用，不重建）
            for tn in task.nodes:
                node = db.get(Node, tn.node_id)
                if node is None:
                    continue
                try:
                    await agent_client.compose_action(node, _task_project(task, tn), "stop")
                    tn.container_status = "exited"
                except Exception as e:
                    errors.append(f"{tn.node_id}: {e}")
        elif action == "start":
            # 启动：docker compose start（复用已停止容器，不重建）。仅当容器已被
            # 清理（旧版 compose down / 外部删除）时回退到 compose up 重建，
            # 保证老任务的 start 也能工作而非卡死。
            if not _task_has_containers(task):
                raise api_error(
                    409, Code.TASK_NOT_RESTARTABLE,
                    "任务没有可启动的容器（可能已被清理），请删除后重新发布",
                )
            for tn in task.nodes:
                node = db.get(Node, tn.node_id)
                if node is None:
                    continue
                try:
                    await agent_client.compose_action(node, _task_project(task, tn), "start")
                    tn.container_status = "running"
                except Exception as e:
                    payload = _task_node_payload(task, tn.node_id)
                    if not payload:
                        errors.append(f"{tn.node_id}: {e}")
                        continue
                    try:
                        # 容器不存在：用首次发布配置恢复（配置未变则启动既有容器）
                        await agent_client.compose_up(
                            node, _task_project(task, tn),
                            payload["compose_yaml"], payload["env"],
                        )
                        tn.container_status = "running"
                    except Exception as e2:
                        errors.append(f"{tn.node_id}: {e2}")
        elif action == "restart":
            # 重启：docker compose restart（进程级重启现有容器，不重建）
            if not _task_has_containers(task):
                raise api_error(
                    409, Code.TASK_NOT_RESTARTABLE,
                    "任务没有运行中的容器，无法重启",
                )
            for tn in task.nodes:
                node = db.get(Node, tn.node_id)
                if node is None:
                    continue
                try:
                    await agent_client.compose_action(node, _task_project(task, tn), "restart")
                    tn.container_status = "running"
                except Exception as e:
                    errors.append(f"{tn.node_id}: {e}")
        elif action == "delete":
            # 删除：compose down（彻底移除容器与网络）+ 可选删除节点模型
            for tn in task.nodes:
                node = db.get(Node, tn.node_id)
                if node is None:
                    continue
                try:
                    await agent_client.compose_down(node, _task_project(task, tn))
                    tn.container_status = "exited"
                except Exception as e:
                    errors.append(f"{tn.node_id}: {e}")
            # 模型与任务解耦：可选在删除时同时删除节点模型（释放磁盘）。
            # 以发布时快照 MODEL_IDS 为准逐个删除；旧任务（无 MODEL_IDS）回退 MODEL_ID。
            head_repos: list[str] = []
            if req.delete_model:
                rendered_nodes = ((task.rendered or {}).get("nodes") or {})
                for payload in rendered_nodes.values():
                    if payload.get("role") == "head":
                        ids = payload.get("env", {}).get("MODEL_IDS")
                        if ids:
                            head_repos = [r for r in str(ids).split(",") if r]
                        else:
                            head_repo = (
                                payload.get("env", {}).get("MODEL_ID")
                                or payload.get("env", {}).get("DSPARK_MODEL")
                            )
                            if head_repo:
                                head_repos = [head_repo]
                        break
                if head_repos:
                    for tn in task.nodes:
                        node = db.get(Node, tn.node_id)
                        for repo in head_repos:
                            try:
                                await agent_client.model_delete(node, repo)
                            except Exception as e:
                                errors.append(f"删除模型 {tn.node_id}: {e}")
        else:
            raise HTTPException(400, f"未知动作: {action}")

        if action == "delete":
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
            _release_task_action_lock(task.id)
            await agent_ws.broadcast({"type": "task_deleted", "task_id": task.id})
            return {"ok": True, "errors": errors,
                    "model_deleted": bool(head_repos) if req.delete_model else False}

        # 容器操作失败处理：pause/resume 只统计有容器节点的失败；stop/start/
        # restart 统计所有有效节点（节点存在）。全部失败时置 error（不虚报成功）。
        if action == "pause":
            manageable = [tn for tn in task.nodes if tn.container_name]
        else:
            manageable = [tn for tn in task.nodes if db.get(Node, tn.node_id) is not None]
        if errors and manageable and len(errors) >= len(manageable):
            task.status = "error"
            task.error = "; ".join(errors)
            try:
                db.commit()
            except StaleDataError:
                raise api_error(409, Code.TASK_STATE_CHANGED,
                                "任务已被删除或状态已变更，请刷新后重试") from None
            await agent_ws.broadcast({"type": "task_status", "task_id": task.id,
                                      "status": "error"})
            return task_to_dict(task)

        if action == "pause":
            task.status = "paused"
        elif action in ("resume", "start", "restart"):
            task.status = "running"
        else:  # stop
            task.status = "stopped"
        task.error = None
        try:
            db.commit()
        except StaleDataError:
            raise api_error(409, Code.TASK_STATE_CHANGED,
                            "任务已被删除或状态已变更，请刷新后重试") from None
        await agent_ws.broadcast({"type": "task_status", "task_id": task.id,
                                  "status": task.status})
        # start/restart 拉起 vLLM 后补健康检查（wait model 加载/端口就绪）
        if action in ("start", "restart"):
            _schedule_task_health_check(db, task)
        return task_to_dict(task)


@router.get("/{task_id}/logs")
async def task_logs(task_id: int, node_id: int, tail: int = 200, db: Session = Depends(get_db)):
    get_task_or_404(db, task_id)  # 404 检查
    # 钳制 tail 区间：超大/负值会让 agent 返回巨型日志体占用内存与带宽
    tail = min(max(int(tail), 1), 5000)
    tn = db.query(TaskNode).filter_by(task_id=task_id, node_id=node_id).first()
    if not tn or not tn.container_name:
        raise api_error(404, Code.CONTAINER_NOT_FOUND, "该节点上无此任务的容器")
    node = db.get(Node, node_id)
    try:
        logs = await agent_client.container_logs(node, tn.container_name, tail)
    except Exception as e:
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
    except Exception as e:
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
