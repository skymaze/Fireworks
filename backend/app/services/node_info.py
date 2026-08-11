"""发布/预览前刷新节点信息，避免用过期硬件快照渲染任务。"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import Node
from . import agent_client


class NodeInfoRefreshError(RuntimeError):
    """一个或多个节点无法返回最新信息。"""


async def refresh_nodes(db: Session, nodes: list[Node]) -> None:
    """并发读取所选节点的 `/api/info`，全部成功后原子更新数据库快照。

    发布参数依赖 GPU、网卡、磁盘等硬件信息，因此任何节点刷新失败都应终止
    本次发布/预览，不能静默回退到可能过期的数据库值。
    """
    if not nodes:
        return
    results = await asyncio.gather(
        *(agent_client.info(node) for node in nodes), return_exceptions=True
    )
    failures = []
    for node, result in zip(nodes, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            failures.append(f"{node.name}: {result}")
        elif not isinstance(result, dict):
            failures.append(f"{node.name}: Agent 返回了无效节点信息")
    if failures:
        raise NodeInfoRefreshError("; ".join(failures))

    now = datetime.now(timezone.utc)
    for node, info in zip(nodes, results, strict=True):
        assert isinstance(info, dict)
        node.hardware_info = info
        node.last_seen = now
    # 节点信息本身是独立状态，即使后续配方校验失败也应保留这次成功刷新。
    db.commit()


async def refresh_nodes_best_effort(
    db: Session, nodes: list[Node], *, retry: bool = False
) -> list[str]:
    """并发刷新可达节点并提交成功结果，返回失败说明。

    用于集群网络已经成功配置并落库后的收尾：单个 Agent 暂时不可达不应让用户误以为
    集群创建失败，但其它节点的新网络/GID 快照仍应立即保存。
    """
    if not nodes:
        return []
    results = await asyncio.gather(
        *(agent_client.info(node, retry=retry) for node in nodes),
        return_exceptions=True,
    )
    failures: list[str] = []
    now = datetime.now(timezone.utc)
    for node, result in zip(nodes, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            failures.append(f"{node.name}: {result}")
            continue
        if not isinstance(result, dict):
            failures.append(f"{node.name}: Agent 返回了无效节点信息")
            continue
        node.hardware_info = result
        node.last_seen = now
    db.commit()
    return failures
