"""LLM 推理统计：端点发现、固定周期原始快照与无损时间桶聚合。

摘要使用完整源区间；max_points 仅约束图表桶数，不丢弃累计计数器增量。
"""

import pytest
from app.db import Base
from app.models import InferenceSample, Node, Task, TaskNode
from app.routers.inference import inference_metrics
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
        "request_success_total": 7.0,
        "kv_cache_percent": 42.5,
        "ttft": {
            "sum": 1.0,
            "count": 10.0,
            "buckets": [[0.05, 1.0], [0.1, 4.0], [None, 10.0]],
        },
        "e2e": None,
        "tpot": {
            "sum": 0.4,
            "count": 8.0,
            "buckets": [[0.05, 2.0], [0.1, 6.0], [None, 8.0]],
        },
    }
    base.update(over)
    return base


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(
        Node(
            id=1,
            name="head",
            ip="192.0.2.1",
            agent_status="online",
            agent_port=9000,
            agent_token="tok",
        )
    )
    db.add(
        Task(
            id=1,
            name="t1",
            recipe_id=1,
            cluster_id=1,
            status="running",
            rendered={
                "nodes": {
                    "1": {
                        "role": "head",
                        "env": {"VLLM_PORT": "8888", "SERVED_MODEL_NAME": "DeepSeek"},
                    },
                }
            },
        )
    )
    db.add(
        TaskNode(
            id=1,
            task_id=1,
            node_id=1,
            role="head",
            node_rank=0,
            container_name="t1-rank0",
        )
    )
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
    db.add(
        Task(
            id=2,
            name="t2",
            recipe_id=1,
            cluster_id=1,
            status="running",
            rendered={"nodes": {"1": {"role": "head", "env": {}}}},
        )
    )
    db.commit()
    assert llm_stats.service_endpoint(db, db.get(Task, 2)) is None
    db.close()


@pytest.mark.anyio
async def test_stats_once_stores_raw_snapshot(env, monkeypatch):
    """落库为原始累计快照（含计数器/直方图 key），不含派生态 tokens_per_sec；不广播。"""

    async def fake(node, payload):
        assert payload["url_base"] == "http://127.0.0.1:8888"
        assert "model" not in payload
        return _snapshot()

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()

    db = env.S()
    sample = db.query(InferenceSample).first()
    assert sample is not None and sample.task_id == 1
    assert sample.model_name is None
    assert sample.data["generation_tokens_total"] == 100.0
    assert sample.data["request_success_total"] == 7.0
    assert sample.data["ttft"]["count"] == 10.0
    assert sample.data["tpot"]["count"] == 8.0
    assert "tokens_per_sec" not in sample.data
    db.close()


@pytest.mark.anyio
async def test_stats_once_keeps_fixed_interval_during_idle(env, monkeypatch):
    """无流量仍保留周期快照，避免下一次流量被整段空闲时间摊薄。"""

    async def fake(node, payload):
        return _snapshot()

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()
    await llm_stats.stats_once()
    db = env.S()
    assert db.query(InferenceSample).count() == 2
    db.close()


@pytest.mark.anyio
async def test_stats_once_compacts_long_idle_period(env, monkeypatch):
    """长空闲期滚动同一个边界点，既保留准确基线又不持续增加行数。"""

    async def fake(node, payload):
        return _snapshot()

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()
    await llm_stats.stats_once()
    db = env.S()
    before = db.query(InferenceSample).order_by(InferenceSample.ts.desc()).first().ts
    db.close()
    await llm_stats.stats_once()
    db = env.S()
    rows = db.query(InferenceSample).order_by(InferenceSample.ts).all()
    assert len(rows) == 2
    assert rows[-1].ts >= before
    db.close()


@pytest.mark.anyio
async def test_stats_once_preserves_kv_peak_before_counters_settle(env, monkeypatch):
    """KV gauge 先升后降时不能被空闲压缩覆盖，即使累计计数器尚未变化。"""
    snapshots = iter(
        [
            _snapshot(kv_cache_percent=0.0),
            _snapshot(kv_cache_percent=80.0),
            _snapshot(kv_cache_percent=0.0),
        ]
    )

    async def fake(node, payload):
        return next(snapshots)

    monkeypatch.setattr(llm_stats.agent_client, "inference_stats", fake)
    await llm_stats.stats_once()
    await llm_stats.stats_once()
    await llm_stats.stats_once()

    db = env.S()
    rows = db.query(InferenceSample).order_by(InferenceSample.ts).all()
    assert [row.data["kv_cache_percent"] for row in rows] == [0.0, 80.0, 0.0]
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
        return {
            "ok": True,
            "backend": "openai",
            "generation_tokens_total": None,
            "prompt_tokens_total": None,
            "request_success_total": None,
            "kv_cache_percent": None,
            "ttft": None,
            "e2e": None,
        }

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


def test_cleanup_legacy_inference_samples_removes_old_format(env):
    """升级清理：只删旧派生格式行，保留新格式（含计数器为 null 的新行）。"""
    db = env.S()
    db.add(InferenceSample(task_id=1, node_id=1, ts=1.0, data={"tokens_per_sec": 1.0}))
    db.add(
        InferenceSample(
            task_id=1,
            node_id=1,
            ts=2.0,
            data={"generation_tokens_total": 10.0},
        )
    )
    db.add(
        InferenceSample(
            task_id=1,
            node_id=1,
            ts=3.0,
            data={"generation_tokens_total": None},
        )
    )
    db.commit()

    assert llm_stats.cleanup_legacy_inference_samples(db) == 1
    rows = db.query(InferenceSample).all()
    assert len(rows) == 2
    assert all("generation_tokens_total" in (r.data or {}) for r in rows)
    # 幂等：再跑一次无事发生
    assert llm_stats.cleanup_legacy_inference_samples(db) == 0
    db.close()


def test_inference_metrics_aggregates_all_intervals_without_discarding(env):
    """max_points 只合并图表桶；摘要仍累计每个源区间并保留原始峰值。"""
    db = env.S()
    ts_list = [95.0, 100.0, 102.0, 104.0, 106.0]
    kv_values = [20.0, 21.0, 80.0, 50.0, 0.0]
    for i, ts in enumerate(ts_list):
        db.add(
            InferenceSample(
                task_id=1,
                node_id=1,
                ts=ts,
                data={
                    "generation_tokens_total": float(i) * 20,
                    "prompt_tokens_total": float(i) * 10,
                    "request_success_total": float(i),
                    "kv_cache_percent": kv_values[i],
                },
            )
        )
    db.commit()

    out = inference_metrics(db=db, from_ts=100, to_ts=106, task_id=1, max_points=2)
    # 100s 是窗口边界基线；之后三个完整源区间均参与。
    assert out["source_intervals"] == 3
    assert len(out["points"]) == 2
    assert sum(point["requests"] for point in out["points"]) == 3
    assert out["summary"]["window_generated_tokens"] == 60
    assert out["summary"]["window_prompt_tokens"] == 30
    assert out["summary"]["window_requests"] == 3
    # 最短 2 秒区间原始峰值为 10 tok/s；不能被桶平均或 max_points 改写。
    assert out["summary"]["decode_peak_tokens_per_sec"] == 10
    assert out["summary"]["request_peak_per_sec"] == 0.5
    # Gauge 按原始采样取窗口峰值、桶内也取最大值，不能被后续空闲 0 覆盖。
    assert out["summary"]["kv_cache_peak_percent"] == 80
    assert [point["kv_cache_percent"] for point in out["points"]] == [80, 50]
    assert out["points"][0]["task_name"] == "t1"
    assert "latest" not in out
    assert "source_samples" not in out
    assert "output_tokens" not in out["points"][0]
    db.close()


def test_inference_metrics_max_points_is_per_series(env):
    """多任务分别获得点数预算，不能把所有任务混抽导致小任务消失。"""
    db = env.S()
    db.add(Task(id=2, name="t2", recipe_id=1, cluster_id=1, status="running"))
    for task_id in (1, 2):
        for i, ts in enumerate((100.0, 105.0, 110.0, 115.0)):
            db.add(
                InferenceSample(
                    task_id=task_id,
                    node_id=1,
                    ts=ts,
                    data={
                        "generation_tokens_total": i * 10,
                        "prompt_tokens_total": i * 5,
                        "request_success_total": i,
                    },
                )
            )
    db.commit()

    out = inference_metrics(db=db, from_ts=100, to_ts=115, task_id=None, max_points=1)
    assert len(out["points"]) == 2
    assert {point["task_id"] for point in out["points"]} == {1, 2}
    assert out["summary"]["window_generated_tokens"] == 60
    db.close()


def test_inference_metrics_prorates_boundary_and_merges_histograms(env):
    """窗口落在采样区间中间时按重叠时长分摊，直方图也按桶累计而非抽点。"""
    db = env.S()
    histogram_rows = [
        (100.0, 0, 0, [0, 0]),
        (110.0, 100, 10, [5, 10]),
        (120.0, 200, 20, [10, 20]),
    ]
    for ts, tokens, count, buckets in histogram_rows:
        db.add(
            InferenceSample(
                task_id=1,
                node_id=1,
                ts=ts,
                data={
                    "generation_tokens_total": tokens,
                    "prompt_tokens_total": tokens / 2,
                    "request_success_total": count,
                    "ttft": {
                        "sum": count,
                        "count": count,
                        "buckets": [[0.1, buckets[0]], [None, buckets[1]]],
                    },
                },
            )
        )
    db.commit()

    out = inference_metrics(db=db, from_ts=105, to_ts=120, task_id=1, max_points=1)
    # 100->110 只与窗口重叠一半，因此 100 token 只计 50；后一段完整计 100。
    assert out["summary"]["window_generated_tokens"] == 150
    assert out["summary"]["decode_average_tokens_per_sec"] == 10
    assert out["summary"]["decode_peak_tokens_per_sec"] == 10
    assert out["summary"]["request_peak_per_sec"] == 1
    # 95% 落在 +Inf 桶时钳制到最后有限边界 100ms。
    assert out["summary"]["ttft_p95_ms"] == 100
    db.close()


def test_inference_metrics_empty_window_uses_null_averages(env):
    """没有有效区间时平均值应为未知，而不是会误导用户的 0 tok/s。"""
    db = env.S()
    out = inference_metrics(db=db, from_ts=100, to_ts=200, task_id=1, max_points=10)
    assert out["points"] == []
    assert out["summary"]["decode_average_tokens_per_sec"] is None
    assert out["summary"]["prefill_average_tokens_per_sec"] is None
    assert out["summary"]["decode_peak_tokens_per_sec"] is None
    db.close()


def test_inference_peak_compares_raw_rates_before_rounding(env):
    """两个展示值相同的速率仍按原始精度选出真正峰值时间。"""
    db = env.S()
    for ts, tokens, kv in (
        (100.0, 0.0, 0.0),
        (110.0, 10.4, 0.005213),
        (120.0, 20.89, 0.0),
    ):
        db.add(
            InferenceSample(
                task_id=1,
                node_id=1,
                ts=ts,
                data={
                    "generation_tokens_total": tokens,
                    "prompt_tokens_total": tokens,
                    "request_success_total": tokens,
                    "kv_cache_percent": kv,
                },
            )
        )
    db.commit()

    out = inference_metrics(db=db, from_ts=100, to_ts=120, task_id=1, max_points=1)
    assert out["summary"]["decode_peak_tokens_per_sec"] == 1.0
    assert out["summary"]["decode_peak_at"] == 120.0
    assert out["summary"]["kv_cache_peak_percent"] == 0.005
    db.close()


def test_inference_metrics_points_emit_volumes_and_latency_percentiles(env):
    """点位输出桶内 token 体量与时延 p50/p95（含 TPOT），替代单值与解码吞吐混排。"""
    db = env.S()
    # 区间内直方图增量累计 [10, 30, 20, 0]：
    #   p50 目标 30 -> 落在 (0.05,0.1] 内 2/3 处；p95 目标 57 -> 落在 (0.1,0.2] 内 85% 处
    hists = {
        "ttft": {"sum": 7.5, "count": 60.0, "buckets": [[0.05, 10.0], [0.1, 40.0], [0.2, 60.0], [None, 60.0]]},
        "e2e": {"sum": 70.0, "count": 60.0, "buckets": [[0.5, 10.0], [1.0, 40.0], [2.0, 60.0], [None, 60.0]]},
        "tpot": {"sum": 1.4, "count": 60.0, "buckets": [[0.01, 10.0], [0.02, 40.0], [0.04, 60.0], [None, 60.0]]},
    }

    def hist_zeros(bounds):
        return {
            "sum": 0.0,
            "count": 0.0,
            "buckets": [[b, 0.0] for b in bounds] + [[None, 0.0]],
        }

    bounds_by = {
        "ttft": [0.05, 0.1, 0.2],
        "e2e": [0.5, 1.0, 2.0],
        "tpot": [0.01, 0.02, 0.04],
    }
    db.add(
        InferenceSample(
            task_id=1,
            node_id=1,
            ts=100.0,
            data={
                "generation_tokens_total": 0.0,
                "prompt_tokens_total": 0.0,
                "request_success_total": 0.0,
                **{key: hist_zeros(bounds_by[key]) for key in hists},
            },
        )
    )
    db.add(
        InferenceSample(
            task_id=1,
            node_id=1,
            ts=110.0,
            data={
                "generation_tokens_total": 1000.0,
                "prompt_tokens_total": 500.0,
                "request_success_total": 7.0,
                **hists,
            },
        )
    )
    db.commit()

    out = inference_metrics(db=db, from_ts=100, to_ts=110, task_id=1, max_points=4)
    point = out["points"][0]
    # 桶内原始 token 体量（不是速率）
    assert point["generated_tokens"] == 1000
    assert point["prompt_tokens"] == 500
    assert point["requests"] == 7
    assert point["ttft_p50_ms"] == 83.3
    assert point["ttft_p95_ms"] == 185.0
    assert point["e2e_p50_ms"] == 833.3
    assert point["e2e_p95_ms"] == 1850.0
    assert point["tpot_p50_ms"] == 16.7
    assert point["tpot_p95_ms"] == 37.0
    db.close()
