"""模型 Agent 高速直传编排回归。"""

import pytest
from app.db import Base
from app.models import Cluster, Node
from app.routers import models as models_router
from app.services import model_manager
from app.services.transfer_selection import validate_roles
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.mark.parametrize(
    "head_id,worker_ids",
    [(None, [2]), (1, [2, 2]), (1, [1, 2])],
)
def test_distribution_roles_must_be_valid(head_id, worker_ids):
    with pytest.raises(HTTPException) as exc:
        validate_roles(head_id, worker_ids)
    assert exc.value.status_code == 422


def _distribution_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_distribution_rejects_cross_cluster():
    db = _distribution_db()
    db.add_all([
        Cluster(id=1, name="cluster-a"),
        Cluster(id=2, name="cluster-b"),
        Node(id=1, name="head", ip="192.0.2.1", cluster_id=1),
        Node(id=2, name="worker", ip="192.0.2.2", cluster_id=2),
    ])
    db.commit()

    req = models_router.DownloadRequest(
        repo="owner/repo", cluster_id=1, head_node_id=1, sync_node_ids=[2],
    )
    with pytest.raises(HTTPException) as exc:
        models_router.validate_distribution_cluster(
            db, req.head_node_id, req.sync_node_ids, req.cluster_id,
        )
    assert exc.value.status_code == 422
    assert "禁止跨集群分发" in str(exc.value.detail)


def test_distribution_accepts_nodes_in_selected_cluster():
    db = _distribution_db()
    db.add(Cluster(id=1, name="cluster-a"))
    db.add_all([
        Node(id=1, name="head", ip="192.0.2.1", cluster_id=1),
        Node(id=2, name="worker", ip="192.0.2.2", cluster_id=1),
    ])
    db.commit()

    models_router.validate_distribution_cluster(db, 1, [2], 1)


@pytest.mark.anyio
async def test_manual_sync_status_rejects_invalid_job_id():
    with pytest.raises(HTTPException) as exc:
        await models_router.sync_status("invalid-job", db=object())
    assert exc.value.status_code == 422


@pytest.mark.anyio
async def test_new_manual_sync_job_id_does_not_require_source_node(monkeypatch):
    worker = Node(id=2, name="worker", ip="192.0.2.2")
    monkeypatch.setattr(models_router, "get_node_or_404", lambda _db, _id: worker)

    async def fetch_status(node, job_id):
        assert node is worker and job_id == "fetch-job"
        return {"status": "completed"}

    monkeypatch.setattr(models_router.agent_client, "model_fetch_status", fetch_status)
    result = await models_router.sync_status("2:fetch-job", db=object())
    assert result["status"] == "completed"


@pytest.mark.anyio
async def test_worker_uses_peer_fetch_and_reports_completion(monkeypatch):
    worker = Node(id=2, name="worker", ip="192.0.2.2", agent_port=9000)
    seen = {}

    async def fake_fetch(node, payload):
        seen.update(node=node, payload=payload)
        return {"job_id": "fetch-1"}

    async def fake_status(node, job_id):
        assert node is worker and job_id == "fetch-1"
        return {"status": "completed", "transferred_bytes": 500}

    monkeypatch.setattr(model_manager.agent_client, "model_fetch", fake_fetch)
    monkeypatch.setattr(model_manager.agent_client, "model_fetch_status", fake_status)
    monkeypatch.setattr(model_manager, "_transfer_is_syncing", lambda _job_id: True)
    monkeypatch.setattr(model_manager, "_update_sync_job", lambda *args: None)

    node_id, result = await model_manager._sync_model_to_worker(
        worker, 42, "owner/repo", [{"type": "file", "relpath": "blobs/a"}],
        500, "http://10.20.0.1:9000/api/model/share/share-id", "short-token",
    )
    assert node_id == 2 and result["status"] == "completed"
    assert seen["node"] is worker
    assert seen["payload"]["source_token"] == "short-token"
    assert seen["payload"]["connections"] == 4


@pytest.mark.anyio
async def test_parent_pause_cancels_worker_fetch(monkeypatch):
    worker = Node(id=2, name="worker", ip="192.0.2.2")
    cancelled = []

    async def fake_fetch(_node, _payload):
        return {"job_id": "fetch-2"}

    async def fake_cancel(_node, job_id):
        cancelled.append(job_id)
        return {"ok": True}

    async def fake_status(_node, job_id):
        assert job_id == "fetch-2"
        return {"status": "cancelled", "transferred_bytes": 200}

    monkeypatch.setattr(model_manager.agent_client, "model_fetch", fake_fetch)
    monkeypatch.setattr(model_manager.agent_client, "model_fetch_cancel", fake_cancel)
    monkeypatch.setattr(model_manager.agent_client, "model_fetch_status", fake_status)
    monkeypatch.setattr(model_manager, "_transfer_is_syncing", lambda _job_id: False)
    monkeypatch.setattr(model_manager, "_update_sync_job", lambda *args: None)

    _, result = await model_manager._sync_model_to_worker(
        worker, 42, "owner/repo", [], 500,
        "http://10.20.0.1:9000/api/model/share/share-id", "short-token",
    )
    assert cancelled == ["fetch-2"] and result["status"] == "paused"


@pytest.mark.anyio
async def test_resume_waits_for_cancelling_fetch_before_restart(monkeypatch):
    worker = Node(id=2, name="worker", ip="192.0.2.2")
    statuses = iter([
        {"status": "cancelling", "transferred_bytes": 300},
        {"status": "cancelled", "transferred_bytes": 300},
        {"status": "completed", "transferred_bytes": 500},
    ])
    started = []

    async def fake_status(_node, _job_id):
        return next(statuses)

    async def fake_fetch(_node, _payload):
        started.append(True)
        return {"job_id": "replacement-fetch"}

    monkeypatch.setattr(model_manager.agent_client, "model_fetch_status", fake_status)
    monkeypatch.setattr(model_manager.agent_client, "model_fetch", fake_fetch)
    monkeypatch.setattr(model_manager, "_transfer_is_syncing", lambda _job_id: True)
    monkeypatch.setattr(model_manager, "_update_sync_job", lambda *args: None)
    monkeypatch.setattr(model_manager, "POLL_INTERVAL", 0)

    _, result = await model_manager._sync_model_to_worker(
        worker, 42, "owner/repo", [], 500,
        "http://10.20.0.1:9000/api/model/share/share-id", "short-token",
        "old-fetch",
    )
    assert started == [True]
    assert result["job_id"] == "replacement-fetch"
    assert result["status"] == "completed"


@pytest.mark.anyio
async def test_resume_reuses_running_agent_fetch(monkeypatch):
    worker = Node(id=2, name="worker", ip="192.0.2.2")
    statuses = iter([
        {"status": "running", "transferred_bytes": 300},
        {"status": "completed", "transferred_bytes": 500},
    ])

    async def fail_new_fetch(_node, _payload):
        raise AssertionError("恢复任务不应创建重复的 Agent fetch")

    async def fake_status(_node, job_id):
        assert job_id == "existing-fetch"
        return next(statuses)

    monkeypatch.setattr(model_manager.agent_client, "model_fetch", fail_new_fetch)
    monkeypatch.setattr(model_manager.agent_client, "model_fetch_status", fake_status)
    monkeypatch.setattr(model_manager, "_transfer_is_syncing", lambda _job_id: True)
    monkeypatch.setattr(model_manager, "_update_sync_job", lambda *args: None)
    monkeypatch.setattr(model_manager, "POLL_INTERVAL", 0)

    _, result = await model_manager._sync_model_to_worker(
        worker, 42, "owner/repo", [], 500,
        "http://10.20.0.1:9000/api/model/share/share-id", "short-token",
        "existing-fetch",
    )
    assert result["status"] == "completed"
    assert result["job_id"] == "existing-fetch"
