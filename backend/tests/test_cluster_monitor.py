"""集群级监控大盘：cluster_metrics 成员降采样、cluster_overview 汇总。"""

import time

import pytest
from app.db import Base
from app.models import Cluster, ClusterNode, MetricSample, Node
from app.routers.clusters import cluster_metrics, cluster_overview
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def env():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(Node(id=1, name="h", ip="192.0.2.1", agent_status="online"))
    db.add(Node(id=2, name="w", ip="192.0.2.2", agent_status="online"))
    db.add(Cluster(id=1, name="c1", network_type="roce"))
    db.add(ClusterNode(id=1, cluster_id=1, node_id=1, net_index=1))
    db.add(ClusterNode(id=2, cluster_id=1, node_id=2, net_index=2))
    db.commit()
    db.close()
    return S


def test_cluster_metrics_returns_per_node_series(env):
    db = env()
    now = time.time()
    db.add(MetricSample(node_id=1, ts=now, data={
        "cpu_percent": 10.0, "gpu": {"utilization": 50.0, "mem_used": 10, "mem_total": 100},
        "temperatures": {"cpu": 40.0}, "network": {"rx_bps": 1, "tx_bps": 2},
    }))
    db.add(MetricSample(node_id=1, ts=now + 1, data={"cpu_percent": 11.0}))
    db.add(MetricSample(node_id=2, ts=now, data={"cpu_percent": 20.0}))
    db.commit()
    db.close()

    out = cluster_metrics(1, now - 10, now + 10, 2000, env())
    members = out["members"]
    assert len(members) == 2
    by_id = {m["node_id"]: m for m in members}
    assert len(by_id[1]["series"]) == 2
    assert by_id[1]["series"][0]["gpu_util"] == 50.0
    assert by_id[1]["agent_status"] == "online"


def test_cluster_metrics_empty_cluster(env):
    db = env()
    db.add(Cluster(id=2, name="c2", network_type="roce"))
    db.commit()
    db.close()
    assert cluster_metrics(2, None, None, 2000, env())["members"] == []


def test_cluster_overview_aggregates(env):
    db = env()
    now = time.time()
    db.add(MetricSample(node_id=1, ts=now, data={
        "gpu": {"utilization": 40.0, "mem_used": 100, "mem_total": 200},
        "temperatures": {"cpu": 45.0}, "network": {"rx_bps": 10, "tx_bps": 20},
    }))
    db.add(MetricSample(node_id=2, ts=now, data={
        "gpu": {"utilization": 60.0, "mem_used": 50, "mem_total": 200},
        "temperatures": {"cpu": 55.0}, "network": {"rx_bps": 30, "tx_bps": 40},
    }))
    db.commit()
    db.close()

    o = cluster_overview(1, env())
    assert o["nodes_total"] == 2 and o["nodes_online"] == 2
    assert o["gpu_util_avg"] == 50.0
    assert o["cpu_temp_avg"] == 50.0
    assert o["gpu_mem_used"] == 150 and o["gpu_mem_total"] == 400
    assert o["net_rx_bps"] == 40 and o["net_tx_bps"] == 60


def test_cluster_overview_excludes_offline(env):
    db = env()
    db.add(Node(id=3, name="off", ip="192.0.2.3", agent_status="offline"))
    db.add(ClusterNode(id=3, cluster_id=1, node_id=3, net_index=3))
    db.commit()
    db.close()
    o = cluster_overview(1, env())
    assert o["nodes_total"] == 3 and o["nodes_online"] == 2
