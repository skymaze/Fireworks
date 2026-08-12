"""总览聚合：资源拓扑、GPU 聚合与并发基准峰值（推理统计已移至前端差分/绘图）。"""

import time

from app.db import Base
from app.models import (
    Cluster,
    MetricSample,
    Node,
    Recipe,
    Task,
    TaskBenchmark,
)
from app.routers.overview import overview
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_overview_aggregates_topology_and_gpu():
    db = _session()
    now = time.time()
    db.add(Cluster(id=1, name="spark", network_type="roce", network_cidr="10.8.0.0/24"))
    db.add(Recipe(id=1, name="vllm", compose_template="services: {}"))
    db.add_all(
        [
            Node(
                id=1,
                name="head",
                ip="192.0.2.1",
                cluster_id=1,
                agent_status="online",
                hardware_info={"gpus": [{"name": "GB10"}]},
            ),
            Node(
                id=2,
                name="worker",
                ip="192.0.2.2",
                cluster_id=1,
                agent_status="offline",
                hardware_info={"gpus": [{"name": "GB10"}]},
            ),
            Task(id=1, name="serve", recipe_id=1, cluster_id=1, status="running"),
        ]
    )
    db.add_all(
        [
            MetricSample(
                node_id=1,
                ts=now - 10,
                data={"gpu": {"utilization": 50, "mem_used": 1024, "mem_total": 4096}},
            ),
            MetricSample(
                node_id=1,
                ts=now - 5,
                data={"gpu": {"utilization": 75, "mem_used": 2048, "mem_total": 4096}},
            ),
            TaskBenchmark(task_id=1, ts=now - 3, result={"tokens_per_sec": 120}),
        ]
    )
    db.commit()

    result = overview(db=db, window=3600)

    assert result.nodes_online == 1 and result.gpu_aggregate["utilization"] == 75
    assert result.gpu_aggregate["total"] == 1
    assert result.gpu_aggregate["mem_used"] == 2048
    assert result.topology_clusters[0].node_ids == [1, 2]
    assert result.topology_nodes[0].cluster_name == "spark"
    assert result.tasks_running == 1
    assert result.benchmark_peak_tokens_per_sec == 120
    db.close()


def test_overview_empty_database():
    db = _session()
    result = overview(db=db, window=3600)
    assert result.nodes_total == 0
    assert result.topology_nodes == []
    assert result.benchmark_peak_tokens_per_sec is None
    db.close()


def test_overview_benchmark_peak_ignores_orphan_and_window():
    """基准峰值忽略孤儿（已删除任务）与窗口外旧结果。"""
    db = _session()
    now = time.time()
    db.add(Cluster(id=1, name="spark", network_type="roce"))
    db.add(Recipe(id=1, name="vllm", compose_template="services: {}"))
    db.add(Node(id=1, name="head", ip="192.0.2.1", cluster_id=1))
    db.add(Task(id=1, name="serve", recipe_id=1, cluster_id=1, status="running"))
    db.add_all(
        [
            TaskBenchmark(task_id=999, ts=now - 10, result={"tokens_per_sec": 9999}),
            TaskBenchmark(task_id=1, ts=now - 7200, result={"tokens_per_sec": 500}),
            TaskBenchmark(task_id=1, ts=now - 10, result={"tokens_per_sec": 123}),
        ]
    )
    db.commit()

    result = overview(db=db, window=3600)
    assert result.benchmark_peak_tokens_per_sec == 123
    db.close()
