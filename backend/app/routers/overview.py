"""总览统计：资源快照、集群拓扑和推理服务性能。"""

import math
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .. import config, schemas
from ..db import get_db
from ..models import (
    Cluster,
    InferenceSample,
    MetricSample,
    Node,
    Recipe,
    Task,
    TaskBenchmark,
)

router = APIRouter(tags=["overview"])
OVERVIEW_POINTS_PER_TASK = 720


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 1)


def _downsample(points: list, limit: int = OVERVIEW_POINTS_PER_TASK) -> list:
    """等距保留首尾点，限制图表载荷；统计值仍使用完整原始样本。"""
    if len(points) <= limit:
        return points
    if limit <= 1:
        return [points[-1]]
    last = len(points) - 1
    return [points[round(i * last / (limit - 1))] for i in range(limit)]


@router.get("/api/overview", response_model=schemas.OverviewOut)
def overview(
    db: Session = Depends(get_db),
    window: Annotated[int, Query(ge=300, le=86400)] = 3600,
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

    task_by_id = {task.id: task for task in tasks}
    inference_rows = (
        db.query(InferenceSample)
        .filter(InferenceSample.ts >= now - window, InferenceSample.ts <= now)
        .order_by(InferenceSample.ts)
        .all()
    )
    points_by_task: dict[int, list[schemas.OverviewInferencePoint]] = {}
    latest_by_task: dict[int, schemas.OverviewInferencePoint] = {}
    token_values: list[float] = []
    ttft_values: list[float] = []
    peak_point: schemas.OverviewInferencePoint | None = None
    for row in inference_rows:
        task = task_by_id.get(row.task_id)
        if task is None:
            continue
        data = row.data or {}
        token_rate = _number(data.get("tokens_per_sec"))
        ttft = _number(data.get("ttft_ms"))
        point = schemas.OverviewInferencePoint(
            ts=row.ts,
            task_id=row.task_id,
            task_name=task.name,
            task_status=task.status,
            model_name=row.model_name,
            backend=str(data.get("backend") or "unknown"),
            tokens_per_sec=token_rate,
            ttft_ms=ttft,
            e2e_ms=_number(data.get("e2e_ms")),
            kv_cache_percent=_number(data.get("kv_cache_percent")),
            preemptions=int(_number(data.get("preemptions")) or 0),
        )
        points_by_task.setdefault(row.task_id, []).append(point)
        latest_by_task[row.task_id] = point
        if token_rate is not None:
            token_values.append(token_rate)
            if peak_point is None or token_rate > (peak_point.tokens_per_sec or 0):
                peak_point = point
        if ttft is not None:
            ttft_values.append(ttft)

    benchmark_rows = (
        db.query(TaskBenchmark)
        .filter(TaskBenchmark.ts >= now - window, TaskBenchmark.ts <= now)
        .all()
    )
    benchmark_peak = None
    for row in benchmark_rows:
        # 防御旧库尚未完成清理或外部写入的孤儿记录，不能让已删除任务影响总览峰值。
        if row.task_id not in task_by_id:
            continue
        rate = _number((row.result or {}).get("tokens_per_sec"))
        if rate is not None and (benchmark_peak is None or rate > benchmark_peak[0]):
            benchmark_peak = (rate, row.ts)

    # “当前值”只使用仍在运行且足够新的探针样本，避免任务停止后把一小时前的
    # 最后一个样本长期展示成实时吞吐。窗口内历史样本仍参与趋势、均值与峰值。
    freshness = max(30, config.LLM_PROBE_INTERVAL * 3)
    current_by_task = {
        task_id: point
        for task_id, point in latest_by_task.items()
        if task_by_id[task_id].status == "running" and point.ts >= now - freshness
    }
    current_rates = [
        point.tokens_per_sec
        for point in current_by_task.values()
        if point.tokens_per_sec is not None
    ]
    current_kv = [
        point.kv_cache_percent
        for point in current_by_task.values()
        if point.kv_cache_percent is not None
    ]
    points = sorted(
        (
            point
            for task_points in points_by_task.values()
            for point in _downsample(task_points)
        ),
        key=lambda point: point.ts,
    )
    inference = schemas.OverviewInference(
        freshness_seconds=freshness,
        monitored_tasks=len(latest_by_task),
        sample_count=len(points),
        current_tokens_per_sec=round(sum(current_rates), 1) if current_rates else None,
        average_tokens_per_sec=(
            round(sum(token_values) / len(token_values), 1) if token_values else None
        ),
        peak_tokens_per_sec=peak_point.tokens_per_sec if peak_point else None,
        peak_at=peak_point.ts if peak_point else None,
        benchmark_peak_tokens_per_sec=benchmark_peak[0] if benchmark_peak else None,
        benchmark_peak_at=benchmark_peak[1] if benchmark_peak else None,
        ttft_p95_ms=_percentile(ttft_values, 0.95),
        kv_cache_percent=(
            round(sum(current_kv) / len(current_kv), 1) if current_kv else None
        ),
        preemptions=sum(point.preemptions or 0 for point in current_by_task.values()),
        series=points,
    )

    return schemas.OverviewOut(
        snapshot_at=now,
        window_seconds=window,
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
        inference=inference,
    )
