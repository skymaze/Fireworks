"""Phase3：推理服务基准测试——POST /tasks/{id}/benchmark 编排、持久化/裁剪、历史查询。"""

import time

import pytest
from app import schemas
from app.db import Base
from app.models import Node, Task, TaskBenchmark, TaskNode
from app.routers import tasks as tasks_router
from app.services import llm_probe
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(Node(id=1, name="head", ip="192.0.2.1", agent_status="online",
                agent_port=9000, agent_token="tok"))
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running",
                rendered={"nodes": {"1": {"role": "head",
                                          "env": {"VLLM_PORT": "8888"}}}}))
    db.add(TaskNode(id=1, task_id=1, node_id=1, role="head", node_rank=0,
                    container_name="t1-rank0"))
    db.commit()
    db.close()

    broadcasted: list[dict] = []

    async def fake_broadcast(msg, exclude=None):
        broadcasted.append(msg)

    monkeypatch.setattr(tasks_router.agent_ws, "is_connected", lambda nid: True)
    monkeypatch.setattr(tasks_router.agent_ws, "broadcast", fake_broadcast)
    # service_endpoint 用真实实现（env 任务含 VLLM_PORT）
    monkeypatch.setattr(tasks_router.llm_probe, "service_endpoint", llm_probe.service_endpoint)
    env.S = S
    env.broadcasted = broadcasted
    return env


def _result():
    return {
        "ok": True, "backend": "vllm", "concurrency": 8, "num_requests": 32,
        "succeeded": 32, "failed": 0, "total_tokens": 900,
        "tokens_per_sec": 55.2, "ttft_p50_ms": 10.0, "ttft_p95_ms": 30.0,
        "e2e_p50_ms": 100.0, "e2e_p95_ms": 200.0,
        "itl_p50_ms": 8.0, "itl_p95_ms": 12.0, "per_request": [],
    }


@pytest.mark.anyio
async def test_run_benchmark_persists_and_broadcasts(env, monkeypatch):
    async def fake_benchmark(node, payload):
        assert payload["url_base"] == "http://127.0.0.1:8888"
        assert payload["concurrency"] == 8
        return _result()

    monkeypatch.setattr(tasks_router.agent_client, "llm_benchmark", fake_benchmark)
    out = await tasks_router.run_task_benchmark(1, schemas.BenchmarkRequest(), env.S())
    assert out["result"]["tokens_per_sec"] == 55.2

    db = env.S()
    row = db.query(TaskBenchmark).first()
    assert row is not None and row.task_id == 1
    assert row.result["tokens_per_sec"] == 55.2
    db.close()
    ev = env.broadcasted[-1]
    assert ev["type"] == "benchmark_result" and ev["task_id"] == 1


@pytest.mark.anyio
async def test_run_benchmark_prunes_old(env, monkeypatch):
    db0 = env.S()
    for i in range(5):
        db0.add(TaskBenchmark(task_id=1, ts=time.time() - 100 + i,
                              result={"ok": False, "n": i}))
    db0.commit()
    db0.close()

    async def fake_benchmark(node, payload):
        return _result()

    monkeypatch.setattr(tasks_router.agent_client, "llm_benchmark", fake_benchmark)
    await tasks_router.run_task_benchmark(1, schemas.BenchmarkRequest(), env.S())

    db = env.S()
    rows = db.query(TaskBenchmark).filter(TaskBenchmark.task_id == 1).all()
    # 5 旧 + 1 新 = 6 -> 裁剪到 BENCHMARK_KEEP(5)，且最新一条在库
    assert len(rows) == 5
    assert any(r.result.get("ok") is True for r in rows)
    db.close()


@pytest.mark.anyio
async def test_run_benchmark_rejects_non_running(env, monkeypatch):
    db = env.S()
    db.get(Task, 1).status = "stopped"
    db.commit()
    db.close()
    with pytest.raises(Exception):
        await tasks_router.run_task_benchmark(1, schemas.BenchmarkRequest(), env.S())


@pytest.mark.anyio
async def test_finished_benchmark_is_not_saved_after_task_deletion(env, monkeypatch):
    async def fake_benchmark(node, payload):
        deleting = env.S()
        deleting.delete(deleting.get(Task, 1))
        deleting.commit()
        deleting.close()
        return _result()

    monkeypatch.setattr(tasks_router.agent_client, "llm_benchmark", fake_benchmark)
    with pytest.raises(Exception) as exc:
        await tasks_router.run_task_benchmark(1, schemas.BenchmarkRequest(), env.S())
    assert getattr(exc.value, "status_code", None) == 409
    db = env.S()
    assert db.query(TaskBenchmark).count() == 0
    db.close()


def test_benchmarks_history_newest_first(env):
    db = env.S()
    db.add(TaskBenchmark(task_id=1, ts=100.0, result={"ok": True, "tokens_per_sec": 1.0}))
    db.add(TaskBenchmark(task_id=1, ts=200.0, result={"ok": True, "tokens_per_sec": 2.0}))
    db.commit()
    db.close()
    out = tasks_router.task_benchmarks(1, 5, env.S())
    assert [r["result"]["tokens_per_sec"] for r in out] == [2.0, 1.0]
