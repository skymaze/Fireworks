"""总览聚合：资源拓扑、推理探针趋势和基准峰值。"""

import time

from app.db import Base
from app.models import (
    Cluster,
    InferenceSample,
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


def test_overview_aggregates_topology_and_inference():
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
                data={
                    "gpu": {"utilization": 50, "mem_used": 1024, "mem_total": 4096},
                },
            ),
            MetricSample(
                node_id=1,
                ts=now - 5,
                data={
                    "gpu": {"utilization": 75, "mem_used": 2048, "mem_total": 4096},
                },
            ),
            InferenceSample(
                task_id=1,
                node_id=1,
                ts=now - 20,
                model_name="Qwen",
                data={
                    "backend": "vllm",
                    "tokens_per_sec": 40,
                    "ttft_ms": 200,
                    "kv_cache_percent": 30,
                    "preemptions": 1,
                },
            ),
            InferenceSample(
                task_id=1,
                node_id=1,
                ts=now - 5,
                model_name="Qwen",
                data={
                    "backend": "vllm",
                    "tokens_per_sec": 60,
                    "ttft_ms": 300,
                    "kv_cache_percent": 45,
                    "preemptions": 2,
                },
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
    assert result.inference.monitored_tasks == 1
    assert result.inference.freshness_seconds >= 30
    assert result.inference.current_tokens_per_sec == 60
    assert result.inference.average_tokens_per_sec == 50
    assert result.inference.peak_tokens_per_sec == 60
    assert result.inference.benchmark_peak_tokens_per_sec == 120
    assert result.inference.ttft_p95_ms == 300
    assert result.inference.kv_cache_percent == 45
    assert result.inference.preemptions == 2
    assert result.inference.series[-1].model_name == "Qwen"
    db.close()


def test_overview_empty_database():
    db = _session()
    result = overview(db=db, window=3600)
    assert result.nodes_total == 0
    assert result.topology_nodes == []
    assert result.inference.sample_count == 0
    assert result.inference.current_tokens_per_sec is None
    db.close()


def test_overview_does_not_report_stale_sample_as_current():
    db = _session()
    now = time.time()
    db.add(Cluster(id=1, name="spark", network_type="roce"))
    db.add(Recipe(id=1, name="vllm", compose_template="services: {}"))
    db.add(Node(id=1, name="head", ip="192.0.2.1", cluster_id=1))
    db.add(Task(id=1, name="serve", recipe_id=1, cluster_id=1, status="stopped"))
    db.add(
        InferenceSample(
            task_id=1,
            node_id=1,
            ts=now - 60,
            model_name="Qwen",
            data={"backend": "vllm", "tokens_per_sec": 80},
        )
    )
    db.commit()

    result = overview(db=db, window=3600)

    assert result.inference.sample_count == 1
    assert result.inference.peak_tokens_per_sec == 80
    assert result.inference.current_tokens_per_sec is None
    db.close()


def test_overview_limits_chart_points_and_benchmark_to_window():
    db = _session()
    now = time.time()
    db.add(Cluster(id=1, name="spark", network_type="roce"))
    db.add(Recipe(id=1, name="vllm", compose_template="services: {}"))
    db.add(Node(id=1, name="head", ip="192.0.2.1", cluster_id=1))
    db.add(Task(id=1, name="serve", recipe_id=1, cluster_id=1, status="running"))
    db.add_all([
        InferenceSample(
            task_id=1, node_id=1, ts=now - 900 + i,
            data={"tokens_per_sec": float(i)},
        )
        for i in range(800)
    ])
    db.add_all([
        TaskBenchmark(task_id=1, ts=now - 7200, result={"tokens_per_sec": 9999}),
        TaskBenchmark(task_id=1, ts=now - 10, result={"tokens_per_sec": 123}),
    ])
    db.commit()

    result = overview(db=db, window=3600)

    assert result.inference.sample_count == 720
    assert len(result.inference.series) == 720
    assert result.inference.series[0].tokens_per_sec == 0
    assert result.inference.series[-1].tokens_per_sec == 799
    assert result.inference.peak_tokens_per_sec == 799
    assert result.inference.benchmark_peak_tokens_per_sec == 123
    db.close()
