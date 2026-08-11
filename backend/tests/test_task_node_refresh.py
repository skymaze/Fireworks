"""任务发布使用 Agent 最新节点信息，而不是数据库旧快照。"""

import pytest
from app.db import Base
from app.models import Cluster, ClusterNode, Node, Recipe
from app.routers.tasks import create_task
from app.schemas import TaskCreate
from app.services import node_info
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.mark.anyio
async def test_publish_refreshes_selected_nodes_before_render(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add_all([
        Cluster(id=1, name="cl", network_type="roce"),
        Node(id=1, name="n1", ip="192.0.2.1", hardware_info={"revision": "old"}),
        Recipe(id=1, name="recipe", compose_template="services: {}", variables=[]),
        ClusterNode(cluster_id=1, node_id=1, net_index=1),
    ])
    db.commit()

    async def fresh_info(node):
        assert node.id == 1
        return {"revision": "fresh", "gpus": [{"name": "GB10"}]}

    def render(recipe, cluster, assignments, variables, task_name):
        assert assignments[0][0].hardware_info["revision"] == "fresh"
        return {"nodes": {"1": {
            "role": "head", "env": {}, "project": task_name,
            "compose_yaml": "services: {}",
        }}}

    async def compose_up(*args, **kwargs):
        return {"ok": True}

    async def compose_ps(*args, **kwargs):
        return {"containers": [{"name": "refresh-rank0", "state": "running"}]}

    async def broadcast(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.node_info.agent_client.info", fresh_info)
    monkeypatch.setattr("app.routers.tasks.recipe_render.render_task", render)
    monkeypatch.setattr("app.routers.tasks.agent_client.compose_up", compose_up)
    monkeypatch.setattr("app.routers.tasks.agent_client.compose_ps", compose_ps)
    monkeypatch.setattr("app.routers.tasks.agent_ws.broadcast", broadcast)

    try:
        result = await create_task(TaskCreate(
            name="refresh", recipe_id=1, cluster_id=1,
            nodes=[{"node_id": 1, "role": "head", "node_rank": 0}],
            send_model=False, send_image=False,
        ), db)
        assert result["status"] == "running"
        db.expire_all()
        node = db.get(Node, 1)
        assert node.hardware_info["revision"] == "fresh"
        assert node.last_seen is not None
    finally:
        db.close()


@pytest.mark.anyio
async def test_node_refresh_failure_never_falls_back_to_stale_snapshot(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(Node(id=1, name="n1", ip="192.0.2.1", hardware_info={"revision": "old"}))
    db.commit()
    node = db.get(Node, 1)

    async def unavailable(_node):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("app.services.node_info.agent_client.info", unavailable)
    try:
        with pytest.raises(node_info.NodeInfoRefreshError, match="n1"):
            await node_info.refresh_nodes(db, [node])
        assert node.hardware_info == {"revision": "old"}
    finally:
        db.close()
