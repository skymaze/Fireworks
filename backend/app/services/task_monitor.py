"""任务容器状态监控。

发布后任务状态为 running，但容器可能随后退出（崩溃/正常结束），
或 compose 健康检查判定为 unhealthy。本模块定期检查各节点容器状态：
- 同步更新 TaskNode.container_status（任务详情页展示真实状态）；
- 运行中任务所有节点容器全部 exited -> 任务状态置为 stopped；
- 运行中任务任一容器 compose healthcheck 为 unhealthy -> 置 error；
- error（健康检查类）任务在容器转 healthy 后恢复 running。

健康判定以 **docker compose 声明的容器 healthcheck 为准**（Agent 通过
`docker inspect` 暴露 Health），不再由控制面写死端口/检查路径；配方未声明
healthcheck 时（health 为空）由调用方按配方信息降级探测。
"""

import asyncio
import logging

from ..db import SessionLocal
from ..models import Node, Task
from . import agent_client

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = 30  # 秒


async def collect_container_health(db, task: Task) -> list[dict]:
    """采集任务各节点容器的 Health（docker compose healthcheck 为准）。

    返回 [{node_name, container, health}]；health 取值：
    "healthy" / "unhealthy" / "starting" / ""（未声明 healthcheck）/
    None（该节点无容器名或采集失败）。
    """
    out: list[dict] = []
    rendered_nodes = ((task.rendered or {}).get("nodes") or {})
    for tn in list(task.nodes):
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
    - healthy   : 至少一个容器有 healthcheck 且全部已 healthy（未声明
                  healthcheck 的容器不阻塞 —— 旧配方降级判定）
    - starting  : 有 healthcheck 但尚未全部 healthy（含启动期）
    - no-check  : 所有容器均未声明 healthcheck（调用方按配方降级探测）
    - unknown   : 无信号 / 采集失败且无任何 health 可依
    """
    checked = [s for s in signals if s.get("health") not in (None, "")]
    if any(s.get("health") == "unhealthy" for s in checked):
        return "unhealthy"
    if any(s.get("health") is None for s in signals):
        # 采集失败（节点暂不可达等）：保守按 starting 继续等待，
        # 避免 head 节点采集失败时其余容器 healthy 被误判为整体健康。
        return "starting"
    if checked:
        if all(s.get("health") == "healthy" for s in checked):
            return "healthy"
        return "starting"
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
                cont = next(
                    (c for c in containers if c.get("name") == tn.container_name), None
                )
                health_signals.append({
                    "node_name": node.name,
                    "container": tn.container_name,
                    "health": ((cont or {}).get("health") or "") if cont else None,
                })
            except Exception as e:
                logger.warning("任务 %s 节点 %s 容器状态检查失败: %s", task.name, node.name, e)
                health_signals.append(
                    {"node_name": node.name, "container": tn.container_name, "health": None}
                )
        db.commit()
        sig_health = aggregate_task_health(health_signals)

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

        # error 任务恢复：健康检查类 error 后，服务可能已就绪（模型加载慢/端口竞态）。
        # 优先按 compose healthcheck：全部 healthy -> 恢复 running；配方未声明
        # healthcheck 时（no-check）回退到 head 的 vLLM API 探测（向后兼容）。
        if (
            task.status == "error"
            and task.error
            and "健康检查" in task.error
        ):
            if sig_health == "healthy":
                try:
                    db.refresh(task)
                except Exception:
                    return
                if task.status == "error":
                    task.status = "running"
                    task.error = None
                    db.commit()
                    logger.info("任务 %s 容器健康已恢复，状态 error -> running", task.name)
                return
            if (
                sig_health == "no-check"
                and states
                and len(states) == len(list(task.nodes))
                and all(s == "running" for s in states)
            ):
                head_node = None
                head_port = "8888"
                for tn in list(task.nodes):
                    if tn.role == "head":
                        head_node = db.get(Node, tn.node_id)
                        head_env = ((task.rendered or {}).get("nodes") or {}).get(
                            str(tn.node_id), {}
                        ).get("env") or {}
                        head_port = head_env.get("VLLM_PORT", "8888")
                        break
                if head_node:
                    try:
                        resp = await agent_client.http_get(
                            head_node, f"http://127.0.0.1:{head_port}/v1/models", timeout=10
                        )
                        if resp.get("status") == 200:
                            task.status = "running"
                            task.error = None
                            db.commit()
                            logger.info("任务 %s 服务已就绪，状态 error -> running", task.name)
                    except Exception as e:
                        logger.warning("任务 %s 恢复检查失败: %s", task.name, e)
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
