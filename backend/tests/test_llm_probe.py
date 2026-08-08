"""Phase2：LLM 探针——推理服务端点发现、探针循环入库/广播、inference-metrics 查询。"""

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import InferenceSample, Node, Task, TaskNode
from app.routers.tasks import task_inference_metrics
from app.services import llm_probe


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(Node(id=1, name="head", ip="192.0.2.1", agent_status="online",
                agent_port=9000, agent_token="tok"))
    db.add(Node(id=2, name="worker", ip="192.0.2.2", agent_status="online",
                agent_port=9000, agent_token="tok"))
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running",
                rendered={
                    "nodes": {
                        "1": {"role": "head", "env": {"VLLM_PORT": "8888",
                                                      "SERVED_MODEL_NAME": "DeepSeek"}},
                        "2": {"role": "worker", "env": {}},
                    }
                }))
    db.add(TaskNode(id=1, task_id=1, node_id=1, role="head", node_rank=0,
                    container_name="t1-rank0"))
    db.commit()
    db.close()

    broadcasted: list[dict] = []

    async def fake_broadcast(msg, exclude=None):
        broadcasted.append(msg)

    monkeypatch.setattr(llm_probe, "SessionLocal", S)
    monkeypatch.setattr(llm_probe.agent_ws, "is_connected", lambda nid: True)
    monkeypatch.setattr(llm_probe.agent_ws, "broadcast", fake_broadcast)
    env.S = S
    env.broadcasted = broadcasted
    return env


def test_service_endpoint_from_rendered(env):
    """含 VLLM_PORT 的 head 渲染 -> (head, url_base, model)。"""
    db = env.S()
    head, url, model = llm_probe.service_endpoint(db, db.get(Task, 1))
    assert head.id == 1
    assert url == "http://127.0.0.1:8888"
    assert model == "DeepSeek"
    db.close()


def test_service_endpoint_none_without_port(env):
    db = env.S()
    db.add(Task(id=2, name="t2", recipe_id=1, cluster_id=1, status="running",
                rendered={"nodes": {"1": {"role": "head", "env": {}}}}))
    db.commit()
    assert llm_probe.service_endpoint(db, db.get(Task, 2)) is None
    db.close()


@pytest.mark.anyio
async def test_probe_once_stores_and_broadcasts(env, monkeypatch):
    result = {
        "ok": True, "backend": "vllm", "tokens_per_sec": 12.3, "ttft_ms": 200.0,
        "e2e_ms": 800.0, "itl_p50_ms": 15.0, "itl_p95_ms": 30.0,
        "kv_cache_percent": 42.5, "preemptions": 3,
        "output_tokens": 16, "prompt_tokens": 9,
    }

    async def fake_probe(node, payload):
        assert payload["url_base"] == "http://127.0.0.1:8888"
        assert payload["model"] == "DeepSeek"
        return result

    monkeypatch.setattr(llm_probe.agent_client, "llm_probe", fake_probe)
    await llm_probe.probe_once()

    db = env.S()
    sample = db.query(InferenceSample).first()
    assert sample is not None and sample.task_id == 1
    assert sample.data["tokens_per_sec"] == 12.3
    assert sample.data["backend"] == "vllm" and sample.data["kv_cache_percent"] == 42.5
    db.close()
    ev = env.broadcasted[-1]
    assert ev["type"] == "inference_metrics" and ev["task_id"] == 1


@pytest.mark.anyio
async def test_probe_once_skips_failure(env, monkeypatch):
    async def fake_probe(node, payload):
        return {"ok": False, "error": "connect fail"}

    monkeypatch.setattr(llm_probe.agent_client, "llm_probe", fake_probe)
    await llm_probe.probe_once()
    db = env.S()
    assert db.query(InferenceSample).count() == 0
    assert env.broadcasted == []
    db.close()


def test_task_inference_metrics_query(env):
    db = env.S()
    now = time.time()
    db.add(InferenceSample(task_id=1, node_id=1, ts=now, data={"tokens_per_sec": 1.0}))
    db.add(InferenceSample(task_id=1, node_id=1, ts=now + 1, data={"tokens_per_sec": 2.0}))
    db.commit()
    db.close()
    # limit<=0 -> 空（与 node_metrics 一致，避免除零）
    assert task_inference_metrics(1, None, None, 0, env.S()) == []
    out = task_inference_metrics(1, now - 10, now + 10, 100, env.S())
    assert len(out) == 2 and out[0]["data"]["tokens_per_sec"] == 1.0
