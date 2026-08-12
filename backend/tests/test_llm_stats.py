"""LLM 推理统计：端点发现 + 原始累计快照落库（无流量不落点、不再广播）。

差分/绘图由前端完成，这里验证后端"只落原始快照 + 无流量跳过"的写侧行为，
以及单查询接口 /api/inference/samples 的时间范围/任务过滤/增量/降采样。
"""

import pytest
from app.db import Base
from app.models import InferenceSample, Node, Task, TaskNode
from app.routers.inference import inference_samples
from app.services import llm_stats
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# +Inf 桶上界在 agent 侧归一化为 None（float('inf') 会序列化成非法的 Infinity）
def _snapshot(**over):
    base = {
        "ok": True,
        "backend": "vllm",
        "generation_tokens_total": 100.0,
        "prompt_tokens_total": 50.0,
        "num_preemptions_total": 3.0,
        "request_success_total": 7.0,
        "kv_cache_percent": 42.5,
        "ttft": {
            "sum": 1.0,
            "count": 10.0,
            "buckets": [[0.05, 1.0], [0.1, 4.0], [None, 10.0]],
        },
        "e2e": None,
    }
    base.update(over)
    return base


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(Node(id=1, name="head", ip="192.0.2.1", agent_status="online",
                agent_port=9000, agent_token="tok"))
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running",
                rendered={
                    "nodes": {
                        "1": {"role": "head", "env": {"VLLM_PORT": "8888",
                                                      "SERVED_MODEL_NAME": "DeepSeek"}},
                    }
                }))
    db.add(TaskNode(id=1, task_id=1, node_id=1, role="head", node_rank=0,
                    container_name="t1-rank0"))
    db.commit()
    db.close()
    monkeypatch.setattr(llm_stats, "SessionLocal", S)
    monkeypatch.setattr(llm_stats.agent_ws, "is_connected", lambda nid: True)
    env.S = S
    return env


def test_service_endpoint_from_rendered(env):
    """含 VLLM_PORT 的 head 渲染 -> (head, url_base, model)。"""
    db = env.S()
    head, url, model = llm_stats.service_endpoint(db, db.get(Task, 1))
    assert head.id == 1
    assert url == "http://127.0.0.1:8888"
    assert model == "DeepSeek"
    db.close()


def test_service_endpoint_none_without_port(env):
    db = env.S()
    db.add(Task(id=2, name="t2", recipe_id=1, cluster_id=1, status="running",
                rendered={"nodes": {"1": {"role": "head", "env": {}}}}))
    db.commit()
    assert llm_stats.service_endpoint(db, db.get(Task, 2)) is None
    db.close()


@pytest.mark.anyio
async def test_stats_once_stores_raw_snapshot(env, monkeypatch):
    """落库为原始累计快照（含计数器/直方图 key），不含派生态 tokens_per_sec；不广播。"""
    async def fake(node, payload):
        assert payload["url_base"] == "http://127.0.0.1:8888"
        assert payload["model"] == "DeepSeek"
        return _snapshot()

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()

    db = env.S()
    sample = db.query(InferenceSample).first()
    assert sample is not None and sample.task_id == 1
    assert sample.model_name == "DeepSeek"
    assert sample.data["generation_tokens_total"] == 100.0
    assert sample.data["num_preemptions_total"] == 3.0
    assert sample.data["request_success_total"] == 7.0
    assert sample.data["ttft"]["count"] == 10.0
    assert "tokens_per_sec" not in sample.data
    db.close()


@pytest.mark.anyio
async def test_stats_once_skips_no_traffic(env, monkeypatch):
    """相邻两份计数器完全相同 -> 无真实流量，只保留首份基线。"""
    async def fake(node, payload):
        return _snapshot()

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()
    await llm_stats.stats_once()
    db = env.S()
    assert db.query(InferenceSample).count() == 1
    db.close()


@pytest.mark.anyio
async def test_stats_once_stores_after_new_traffic(env, monkeypatch):
    """计数器有增量（真实流量）-> 落第二份快照。"""
    snapshots = [
        _snapshot(),
        _snapshot(generation_tokens_total=200.0, request_success_total=12.0),
    ]
    it = iter(snapshots)

    async def fake(node, payload):
        return next(it)

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()
    await llm_stats.stats_once()
    db = env.S()
    assert db.query(InferenceSample).count() == 2
    db.close()


@pytest.mark.anyio
async def test_stats_once_skips_failure(env, monkeypatch):
    async def fake(node, payload):
        return {"ok": False, "error": "connect fail"}

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()
    db = env.S()
    assert db.query(InferenceSample).count() == 0
    db.close()


@pytest.mark.anyio
async def test_stats_once_skips_non_vllm(env, monkeypatch):
    """非 vLLM 后端（无计数器）不产生数据点。"""
    async def fake(node, payload):
        return {"ok": True, "backend": "openai",
                "generation_tokens_total": None, "prompt_tokens_total": None,
                "num_preemptions_total": None, "request_success_total": None,
                "kv_cache_percent": None, "ttft": None, "e2e": None}

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()
    db = env.S()
    assert db.query(InferenceSample).count() == 0
    db.close()


@pytest.mark.anyio
async def test_stats_result_is_discarded_if_task_was_deleted(env, monkeypatch):
    """Agent 请求期间任务被删除 -> 锁内复查失败，不产生孤儿样本。"""
    async def fake(node, payload):
        deleting = env.S()
        deleting.delete(deleting.get(Task, 1))
        deleting.commit()
        deleting.close()
        return _snapshot()

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()
    db = env.S()
    assert db.query(InferenceSample).count() == 0
    db.close()


def test_inference_samples_query(env):
    """单查询接口：升序 / 任务过滤 / 增量 / 降采样（末尾保留）。"""
    db = env.S()
    ts_list = [100.0, 102.0, 104.0, 106.0]
    for i, ts in enumerate(ts_list):
        db.add(InferenceSample(task_id=1, node_id=1, ts=ts,
                               data={"generation_tokens_total": float(i) * 10}))
    db.commit()

    out = inference_samples(db=db, from_ts=0, to_ts=200, task_id=None, limit=100)
    assert [r["ts"] for r in out] == ts_list
    assert out[0]["task_name"] == "t1"

    assert len(inference_samples(db=db, from_ts=0, to_ts=200, task_id=1, limit=100)) == 4
    # 增量：from 传上次最后 ts 之后 -> 只返回新样本
    inc = inference_samples(db=db, from_ts=102.5, to_ts=200, task_id=None, limit=100)
    assert [r["ts"] for r in inc] == [104.0, 106.0]
    # 降采样 limit=2 保留首尾（最新保留）
    sampled = inference_samples(db=db, from_ts=0, to_ts=200, task_id=None, limit=2)
    assert [r["ts"] for r in sampled] == [100.0, 106.0]
    db.close()
