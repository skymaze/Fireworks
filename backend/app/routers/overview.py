"""总览统计：资源快照与集群拓扑。

推理统计由 /api/inference/metrics 对累计快照做差分与时间桶聚合。
"""

import math
import time

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..models import (
    Cluster,
    MetricSample,
    Node,
    Recipe,
    Task,
)

router = APIRouter(tags=["overview"])


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


@router.get("/api/overview", response_model=schemas.OverviewOut)
def overview(
    db: Session = Depends(get_db),
):
    now = time.time()
    nodes = db.query(Node).all()
    clusters = db.query(Cluster).all()
    tasks = db.query(Task).all()
    online = [n for n in nodes if n.agent_status == "online"]
    gpu_total = sum(len((n.hardware_info or {}).get("gpus", [])) for n in online)

    # 一次联表取得每个节点最新样本，避免按在线节点逐个查询造成 N+1。
    latest_ts = (
        db.query(MetricSample.node_id, func.max(MetricSample.ts).label("max_ts"))
        .group_by(MetricSample.node_id)
        .subquery()
    )
    latest_rows = (
        db.query(MetricSample)
        .join(
            latest_ts,
            and_(
                MetricSample.node_id == latest_ts.c.node_id,
                MetricSample.ts == latest_ts.c.max_ts,
            ),
        )
        .all()
    )
    latest_metrics = {row.node_id: row.data or {} for row in latest_rows}

    util_sum = 0.0
    util_count = 0
    mem_used = mem_total = 0
    for n in online:
        g = (latest_metrics.get(n.id) or {}).get("gpu") or {}
        util = _number(g.get("utilization"))
        if util is not None:
            util_sum += util
            util_count += 1
        mem_used += int(_number(g.get("mem_used")) or 0)
        mem_total += int(_number(g.get("mem_total")) or 0)

    cluster_by_id = {cluster.id: cluster for cluster in clusters}
    topology_nodes = []
    for node in nodes:
        metric_gpu = (latest_metrics.get(node.id) or {}).get("gpu") or {}
        cluster = cluster_by_id.get(node.cluster_id)
        topology_nodes.append(
            schemas.OverviewTopologyNode(
                id=node.id,
                name=node.name,
                ip=node.ip,
                status=node.agent_status,
                cluster_id=node.cluster_id,
                cluster_name=cluster.name if cluster else None,
                gpu_count=len((node.hardware_info or {}).get("gpus", [])),
                gpu_utilization=_number(metric_gpu.get("utilization")),
                gpu_mem_used=int(_number(metric_gpu.get("mem_used")) or 0),
                gpu_mem_total=int(_number(metric_gpu.get("mem_total")) or 0),
            )
        )

    topology_clusters = [
        schemas.OverviewTopologyCluster(
            id=cluster.id,
            name=cluster.name,
            network_type=cluster.network_type,
            network_cidr=cluster.network_cidr,
            node_ids=sorted(node.id for node in nodes if node.cluster_id == cluster.id),
        )
        for cluster in clusters
    ]

    return schemas.OverviewOut(
        snapshot_at=now,
        nodes_total=len(nodes),
        nodes_online=len(online),
        clusters_total=len(clusters),
        recipes_total=db.query(Recipe).count(),
        tasks_total=len(tasks),
        tasks_running=sum(1 for t in tasks if t.status == "running"),
        tasks_paused=sum(1 for t in tasks if t.status == "paused"),
        gpu_aggregate={
            "total": gpu_total,
            "utilization": round(util_sum / util_count, 1) if util_count else None,
            "mem_used": mem_used,
            "mem_total": mem_total,
        },
        topology_nodes=topology_nodes,
        topology_clusters=topology_clusters,
    )
