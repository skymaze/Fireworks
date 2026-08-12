"""总览聚合：资源拓扑与 GPU 聚合（推理统计由专用接口聚合）。"""

import time

from app.db import Base
from app.models import (
    Cluster,
    MetricSample,
    Node,
    Recipe,
    Task,
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
        ]
    )
    db.commit()

    result = overview(db=db)

    assert result.nodes_online == 1 and result.gpu_aggregate["utilization"] == 75
    assert result.gpu_aggregate["total"] == 1
    assert result.gpu_aggregate["mem_used"] == 2048
    assert result.topology_clusters[0].node_ids == [1, 2]
    assert result.topology_nodes[0].cluster_name == "spark"
    assert result.tasks_running == 1
    db.close()


def test_overview_empty_database():
    db = _session()
    result = overview(db=db)
    assert result.nodes_total == 0
    assert result.topology_nodes == []
    db.close()
