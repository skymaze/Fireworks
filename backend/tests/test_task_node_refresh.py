"""任务发布使用 Agent 最新节点信息，而不是数据库旧快照。"""

import pytest
from app.db import Base
from app.models import Cluster, ClusterNode, Node, Recipe, Task
from app.routers.tasks import create_task
from app.schemas import TaskCreate
from app.services import node_info
from fastapi import HTTPException
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


@pytest.mark.anyio
async def test_concurrent_publish_atomically_reserves_selected_node(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tasks.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    with S() as seed:
        seed.add_all([
            Cluster(id=1, name="cl", network_type="roce"),
            Node(id=1, name="n1", ip="192.0.2.1"),
            Recipe(id=1, name="recipe", compose_template="services: {}", variables=[]),
            ClusterNode(cluster_id=1, node_id=1, net_index=1),
        ])
        seed.commit()

    arrived = 0
    both_refreshing = __import__("asyncio").Event()

    async def fresh_info(node):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_refreshing.set()
        await both_refreshing.wait()
        return {"revision": "fresh"}

    def render(recipe, cluster, assignments, variables, task_name):
        return {"nodes": {"1": {
            "role": "head", "env": {}, "project": task_name,
            "compose_yaml": "services: {}",
        }}}

    async def compose_up(*args, **kwargs):
        return {"ok": True}

    async def compose_ps(node, project):
        return {"containers": [{"name": f"{project}-rank0", "state": "running"}]}

    async def broadcast(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.node_info.agent_client.info", fresh_info)
    monkeypatch.setattr("app.routers.tasks.recipe_render.render_task", render)
    monkeypatch.setattr("app.routers.tasks.agent_client.compose_up", compose_up)
    monkeypatch.setattr("app.routers.tasks.agent_client.compose_ps", compose_ps)
    monkeypatch.setattr("app.routers.tasks.agent_ws.broadcast", broadcast)

    async def publish(name):
        db = S()
        try:
            return await create_task(TaskCreate(
                name=name, recipe_id=1, cluster_id=1,
                nodes=[{"node_id": 1, "role": "head", "node_rank": 0}],
                send_model=False, send_image=False,
            ), db)
        finally:
            db.close()

    results = await __import__("asyncio").gather(
        publish("first"), publish("second"), return_exceptions=True
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    rejected = next(result for result in results if isinstance(result, BaseException))
    assert isinstance(rejected, HTTPException) and rejected.status_code == 409


@pytest.mark.anyio
async def test_publish_rejects_recipe_change_during_image_preparation(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add_all([
        Cluster(id=1, name="cl", network_type="roce"),
        Node(id=1, name="n1", ip="192.0.2.1"),
        Recipe(
            id=1,
            name="recipe",
            compose_template="services: {}",
            variables=[{"key": "IMAGE", "picker": "image"}],
        ),
        ClusterNode(cluster_id=1, node_id=1, net_index=1),
    ])
    db.commit()

    async def fresh_info(_node):
        return {"revision": "fresh"}

    def render(_recipe, _cluster, _assignments, _variables, task_name):
        return {"nodes": {"1": {
            "role": "head", "env": {"IMAGE": "example/image:latest"},
            "project": task_name, "compose_yaml": "services: {}",
        }}}

    async def ensure_image(*_args, **_kwargs):
        recipe = db.get(Recipe, 1)
        recipe.compose_template = "services: {changed: {}}"
        db.commit()
        return {"ok": True}

    monkeypatch.setattr("app.services.node_info.agent_client.info", fresh_info)
    monkeypatch.setattr("app.routers.tasks.recipe_render.render_task", render)
    monkeypatch.setattr("app.services.image_manager.ensure_image_on_nodes", ensure_image)

    try:
        with pytest.raises(HTTPException) as exc_info:
            await create_task(TaskCreate(
                name="stale-render", recipe_id=1, cluster_id=1,
                nodes=[{"node_id": 1, "role": "head", "node_rank": 0}],
                send_model=False, send_image=True,
            ), db)
        assert exc_info.value.status_code == 409
        assert db.query(Task).count() == 0
    finally:
        db.close()
