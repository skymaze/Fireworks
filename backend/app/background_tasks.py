"""后台任务治理：集中托管 fire-and-forget asyncio 任务。

问题：CPython 对无引用的 pending Task 可能在 GC 时被销毁（
"Task was destroyed but it is pending!"），导致模型/镜像传输监控
（_monitor_job / _monitor_transfer）等长任务静默中断；其异常若不 attend
也只留一条 "Task exception was never retrieved" 日志，对用户不可见。

本模块保存强引用、挂载异常回调，并允许 lifespan 关停时统一取消等待。
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """创建并登记一个后台任务（返回 task 供调用方可选使用）。"""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("后台任务异常 %s: %s", task.get_name(), exc)


def cancel_all() -> None:
    """lifespan 关停：取消全部已登记任务（不等待）。"""
    for task in list(_tasks):
        task.cancel()


async def wait_all() -> None:
    """等待全部已登记任务结束（配合 cancel_all 使用）。"""
    pending = list(_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _tasks.clear()
