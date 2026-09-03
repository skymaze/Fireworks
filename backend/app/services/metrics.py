"""指标轮询（纯数据兜底）：仅对 WS 未连接的节点走 HTTP 拉样本入库，补重连窗口的
数据缺口；节点 online/offline 状态由 agent_ws 的 WS 连接 + 心跳看门狗单一写入，
本模块不参与状态判定（避免双写竞争）。并按保留期清理旧样本。"""

import asyncio
import logging
import time

from sqlalchemy import delete

from .. import config
from ..db import SessionLocal
from ..models import InferenceSample, MetricSample, Node
from . import agent_client, agent_ws

logger = logging.getLogger(__name__)

# 过期样本清理节流：不必每轮轮询都全表 DELETE（全量删+插是主要 SQLite 写压力源）
CLEANUP_INTERVAL = 60  # 秒
_last_cleanup = 0.0


async def poll_once() -> None:
    global _last_cleanup
    # 先在短会话里读节点，探测/写库阶段不持连接：全节点离线时逐个探测
    # （每个最多 3 次重试 × 5s 连接超时）整轮可达数分钟，持会话等待会把
    # 连接池占满拖垮整个后端。
    with SessionLocal() as db:
        nodes = db.query(Node).all()
    now = time.time()
    pending = [n for n in nodes if not agent_ws.is_connected(n.id)]
    for node in pending:
        try:
            m = await agent_client.metrics(node)
        except Exception as e:
            logger.warning("node %s metrics failed: %s", node.name, e)
            continue
        # 节点实例已随会话关闭分离（列属性已加载仍可读）；写库用短会话
        with SessionLocal() as db:
            db.add(
                MetricSample(
                    node_id=node.id,
                    ts=m.get("ts", now),
                    data=m,
                )
            )
            db.commit()

    # 清理过期样本（节流：每 CLEANUP_INTERVAL 一次）
    if now - _last_cleanup >= CLEANUP_INTERVAL:
        _last_cleanup = now
        cutoff = now - config.METRIC_RETENTION_HOURS * 3600
        with SessionLocal() as db:
            db.execute(delete(MetricSample).where(MetricSample.ts < cutoff))
            inference_cutoff = now - config.INFERENCE_RETENTION_HOURS * 3600
            db.execute(delete(InferenceSample).where(InferenceSample.ts < inference_cutoff))
            db.commit()


async def metrics_loop() -> None:
    while True:
        try:
            await poll_once()
        except Exception:
            logger.exception("指标轮询失败")
        await asyncio.sleep(config.METRIC_POLL_INTERVAL)
