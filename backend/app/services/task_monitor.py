"""任务容器状态监控。

发布后任务状态为 running，但容器可能随后退出（崩溃/正常结束）。
本模块定期检查各节点容器状态：
- 同步更新 TaskNode.container_status（任务详情页展示真实状态）；
- 运行中任务所有节点容器全部 exited -> 任务状态置为 stopped。
"""

import asyncio
import logging

from ..db import SessionLocal
from ..models import Node, Task
from . import agent_client

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = 30  # 秒


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
        for tn in list(task.nodes):
            node = db.get(Node, tn.node_id)
            if not node:
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
            except Exception as e:  # noqa: BLE001
                logger.warning("任务 %s 节点 %s 容器状态检查失败: %s", task.name, node.name, e)
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
            except Exception:  # noqa: BLE001 - 任务已被删除
                return
            if task.status == "running":
                task.status = "stopped"
                db.commit()
                logger.info("任务 %s 容器已全部退出，状态 -> stopped", task.name)
            return
        # error 任务恢复：健康检查超时转 error 后，服务可能已就绪（模型加载慢/端口竞态）。
        # 容器全部 running 且 head 的 vLLM API 可访问 -> 恢复 running。
        if (
            task.status == "error"
            and task.error
            and "健康检查超时" in task.error
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
                except Exception as e:  # noqa: BLE001
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
                except Exception:  # noqa: BLE001
                    logger.exception("任务容器状态检查异常 task=%s", tid)
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            logger.exception("启动恢复检查异常 task=%s", tid)
    return len(task_ids)
