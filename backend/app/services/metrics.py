"""指标轮询：后台循环采集各节点 Agent 指标入库，并按保留期清理旧样本。"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import delete

from .. import config
from ..db import SessionLocal
from ..models import MetricSample, Node
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
                if node.agent_status != "offline":
                    node.agent_status = "offline"
                    db.commit()
                logger.warning("node %s metrics failed: %s", node.name, e)
                continue
            node.agent_status = "online"
            node.last_seen = datetime.now(timezone.utc)
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
            db.commit()


async def metrics_loop() -> None:
    while True:
        try:
            await poll_once()
        except Exception:  # noqa: BLE001
            logger.exception("指标轮询失败")
        await asyncio.sleep(config.METRIC_POLL_INTERVAL)
