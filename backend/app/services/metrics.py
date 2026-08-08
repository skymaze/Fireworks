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
    with SessionLocal() as db:
        nodes = db.query(Node).all()
        now = time.time()
        for node in nodes:
            if agent_ws.is_connected(node.id):
                continue  # WS 推送已实时入库，跳过 HTTP 轮询避免双写
            try:
                m = await agent_client.metrics(node)
            except Exception as e:  # noqa: BLE001
                logger.warning("node %s metrics failed: %s", node.name, e)
                continue
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
            db.execute(delete(MetricSample).where(MetricSample.ts < cutoff))
            db.execute(delete(InferenceSample).where(InferenceSample.ts < cutoff))
            db.commit()


async def metrics_loop() -> None:
    while True:
        try:
            await poll_once()
        except Exception:  # noqa: BLE001
            logger.exception("指标轮询失败")
        await asyncio.sleep(config.METRIC_POLL_INTERVAL)
