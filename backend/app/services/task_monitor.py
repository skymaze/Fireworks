"""任务容器状态监控。

节点只有**容器状态**（TaskNode.container_status）；**健康属于任务层面**
（Task.health，由 head 容器 compose healthcheck 聚合）。
本模块定期检查各节点容器状态：
- 同步更新 TaskNode.container_status（详情页容器表展示）；
  并按 head 容器健康聚合写入 Task.health（healthy/unhealthy/starting/未配置）；
- 运行中任务所有节点容器全部 exited -> 任务状态置为 stopped；
- head 容器 compose healthcheck 判定 unhealthy -> 任务置 error；
- error（健康检查类）任务在 head 容器转 healthy 后恢复 running。

健康判定以 **docker compose 声明的容器 healthcheck 为准**（Agent 通过
`docker inspect` 暴露 Health），且**只以 head 容器为准**（worker 没有
健康端点、不参与判定）；配方未声明 healthcheck（health 为空）时健康状态
即为「未配置」。
"""

import asyncio
import logging

from ..db import SessionLocal
from ..models import Node, Task
from . import agent_client

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = 30  # 秒


async def collect_container_health(db, task: Task) -> list[dict]:
    """采集任务 **head 节点**容器的 Health（docker compose healthcheck 为准）。

    健康判定只以 head 为准：对外提供 /health 与服务的是 head，worker 没有
    健康端点、不参与健康判定。返回 [{node_name, container, health}]；
    health 取值：
    "healthy" / "unhealthy" / "starting" / ""（未配置 healthcheck）/
    None（采集失败，不参与判定）。
    """
    out: list[dict] = []
    rendered_nodes = ((task.rendered or {}).get("nodes") or {})
    for tn in list(task.nodes):
        if tn.role != "head":
            continue
        node = db.get(Node, tn.node_id)
        if not node:
            continue
        if not tn.container_name:
            continue
        project = rendered_nodes.get(str(tn.node_id), {}).get("project") or task.name
        try:
            ps = await agent_client.compose_ps(node, project)
            cont = next(
                (c for c in ps.get("containers", []) if c.get("name") == tn.container_name),
                None,
            )
            out.append({
                "node_name": node.name,
                "container": tn.container_name,
                "health": ((cont or {}).get("health") or "") if cont else None,
            })
        except Exception as e:  # 节点不可达等，不阻断其它节点
            logger.warning("任务 %s 节点 %s 容器健康采集失败: %s",
                           task.name, node.name, e)
            out.append({"node_name": node.name,
                        "container": tn.container_name, "health": None})
    return out


def aggregate_task_health(signals: list[dict]) -> str:
    """聚合任务容器健康信号（compose healthcheck 为准）。

    - unhealthy : 任一容器 healthcheck 判定 unhealthy（docker 稳定失败态）
    - healthy   : 声明的容器全部 healthy（未配置 healthcheck 的容器不参与判定）
    - starting  : 有声明但尚未全部 healthy（含启动期）
    - no-check  : 所有容器均未声明 healthcheck（详情页显示「未配置」）
    """
    values = [s.get("health") for s in signals if s.get("health") not in (None, "")]
    if any(v == "unhealthy" for v in values):
        return "unhealthy"
    if values:
        return "healthy" if all(v == "healthy" for v in values) else "starting"
    return "no-check"


async def _check_task(task_id: int) -> None:
    """检查任务所有节点的容器状态并同步 DB。"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            return
        was_running = task.status == "running"
        rendered = task.rendered or {}
        states: list[str] = []
        health_signals: list[dict] = []
        for tn in list(task.nodes):
            node = db.get(Node, tn.node_id)
            if not node:
                continue
            if not tn.container_name:
                continue
            project = (rendered.get("nodes") or {}).get(str(tn.node_id), {}).get("project") or task.name
            try:
                ps = await agent_client.compose_ps(node, project)
                containers = ps.get("containers", [])
                if containers:
                    st = containers[0].get("state", "")
                    if st:
                        tn.container_status = st
                        states.append(st)
                # 健康只取 head 容器（任务层面判定；worker 只有状态）
                if tn.role == "head":
                    cont = next(
                        (c for c in containers if c.get("name") == tn.container_name), None
                    )
                    health = ((cont or {}).get("health") or "") if cont else ""
                    health_signals.append({
                        "node_name": node.name,
                        "container": tn.container_name,
                        "health": health or None,
                    })
            except Exception as e:
                logger.warning("任务 %s 节点 %s 容器状态检查失败: %s", task.name, node.name, e)
                if tn.role == "head":
                    health_signals.append(
                        {"node_name": node.name, "container": tn.container_name, "health": None}
                    )
        sig_health = aggregate_task_health(health_signals)
        # 任务层面健康快照（节点只有状态；健康属于任务）
        task.health = "" if sig_health == "no-check" else sig_health
        db.commit()

        # 运行中任务：所有节点的容器均已退出 -> 任务停止。
        # 写状态前复查当前 DB 状态（用户 pause/stop 或 WS 事件已处理时不覆盖）
        if (
            was_running
            and states
            and len(states) == len(list(task.nodes))
            and all(s == "exited" for s in states)
        ):
            try:
                db.refresh(task)
            except Exception:
                return
            if task.status == "running":
                task.status = "stopped"
                db.commit()
                logger.info("任务 %s 容器已全部退出，状态 -> stopped", task.name)
            return

        # 运行中任务：任一容器 compose healthcheck 判定 unhealthy -> 置 error
        if was_running and sig_health == "unhealthy":
            try:
                db.refresh(task)
            except Exception:
                return
            if task.status == "running":
                bad = next((s for s in health_signals
                            if s.get("health") == "unhealthy"), None)
                task.status = "error"
                task.error = (f"容器健康检查失败"
                              + (f"：{bad['node_name']}（{bad['container']}）" if bad else ""))
                db.commit()
                logger.warning("任务 %s %s，状态 -> error", task.name, task.error)
            return

        # error 任务恢复：健康检查类 error（如模型加载慢导致超时）后，
        # 容器健康转 healthy -> 恢复 running。
        if (
            task.status == "error"
            and task.error
            and "健康检查" in task.error
            and sig_health == "healthy"
        ):
            try:
                db.refresh(task)
            except Exception:
                return
            if task.status == "error":
                task.status = "running"
                task.error = None
                db.commit()
                logger.info("任务 %s 容器健康已恢复，状态 error -> running", task.name)
    finally:
        db.close()


async def task_monitor_loop() -> None:
    """循环监控所有任务的容器状态。"""
    while True:
        try:
            db = SessionLocal()
            try:
                task_ids = [t.id for t in db.query(Task).all()]
            finally:
                db.close()
            for tid in task_ids:
                try:
                    await _check_task(tid)
                except Exception:
                    logger.exception("任务容器状态检查异常 task=%s", tid)
        except Exception:
            logger.exception("任务容器监控异常")
        await asyncio.sleep(MONITOR_INTERVAL)


async def resume_task_monitors() -> int:
    """后端重启后对存量任务立即检查一次（不等第一个周期）。

    首轮内联 await 执行（task_monitor_loop 首个周期前完成），避免与循环各自
    创建 _check_task 造成同一任务的重复并发检查/竞争写状态。
    """
    db = SessionLocal()
    try:
        task_ids = [t.id for t in db.query(Task).all()]
    finally:
        db.close()
    for tid in task_ids:
        try:
            await _check_task(tid)
        except Exception:
            logger.exception("启动恢复检查异常 task=%s", tid)
    return len(task_ids)
