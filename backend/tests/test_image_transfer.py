"""镜像高速传输编排回归：权威网络 IP、短期令牌和 Agent 直拉。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Cluster, ClusterNode, Node
from app.services import image_manager, peer_transfer


def test_node_transfer_ip_prefers_cluster_plan():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    node = Node(
        id=1, name="head", ip="192.0.2.10",
        hardware_info={"roce": [{"rocev2_ip": "10.99.0.9"}]},
    )
    cluster = Cluster(
        id=1, name="c1", network_type="roce",
        network_plan={
            "iface_subnets": {"enp1s0f0np0": "10.20.0.0/24"},
            "cidr": "10.20.0.0/24", "mtu": 9000,
        },
    )
    db.add_all([node, cluster])
    db.add(ClusterNode(cluster_id=1, node_id=1, net_index=3))
    db.commit()

    assert peer_transfer.node_transfer_ip(db, node) == "10.20.0.12"
    db.close()


@pytest.mark.anyio
async def test_worker_fetch_uses_head_share_token(monkeypatch):
    worker = Node(id=2, name="worker", ip="192.0.2.11", agent_port=9000)
    seen = {}

    async def fake_fetch(node, payload):
        seen.update(node=node, payload=payload)
        return {"ok": True, "bytes": 500}

    monkeypatch.setattr(image_manager.agent_client, "image_fetch", fake_fetch)
    node_id, result = await image_manager._sync_archive_to_worker(
        worker, 42, "example/image:1", "sha256:abc", 500,
        "http://10.20.0.1:9000/api/image/share/sha256:abc", "short-token",
    )

    assert node_id == worker.id and result["status"] == "completed"
    assert seen["node"] is worker
    assert seen["payload"]["source_url"] == (
        "http://10.20.0.1:9000/api/image/share/sha256:abc"
    )
    assert seen["payload"]["source_token"] == "short-token"
    assert seen["payload"]["transfer_id"] == 42


@pytest.mark.anyio
async def test_current_agent_capability_is_accepted(monkeypatch):
    node = Node(id=1, name="n1", ip="192.0.2.1")

    async def fake_info(_node):
        return {"capabilities": ["image_peer_transfer_v1"]}

    monkeypatch.setattr(image_manager.agent_client, "info", fake_info)
    assert await peer_transfer.check_agent_capability(
        node, image_manager.agent_client, "image_peer_transfer_v1",
    ) is None


@pytest.mark.anyio
async def test_missing_agent_capability_fails_without_mutation(monkeypatch):
    node = Node(id=1, name="outdated", ip="192.0.2.1")

    async def fake_info(_node):
        return {"agent_version": "0.1.0"}

    monkeypatch.setattr(image_manager.agent_client, "info", fake_info)
    error = await peer_transfer.check_agent_capability(
        node, image_manager.agent_client, "image_peer_transfer_v1",
    )
    assert "重新部署 Agent" in error
