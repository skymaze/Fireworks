"""将 vLLM 累计快照差分并按时间桶聚合。

`max_points` 只控制每条任务/节点序列的输出分辨率。所有源区间仍参与 token、
请求和直方图累计，窗口合计与峰值不会因为图表降采样而丢失。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

from ..models import InferenceSample


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _round1(value: float) -> float:
    return round(value, 1)


def _counter_delta(prev: Any, cur: Any) -> float | None:
    a = _number(prev)
    b = _number(cur)
    if a is None or b is None or b < a:
        return None
    return b - a


def _hist_delta(prev: dict | None, cur: dict | None) -> dict | None:
    """累计直方图差分；计数器回退或桶定义变化时返回 None。"""
    prev_buckets = (prev or {}).get("buckets") or []
    cur_buckets = (cur or {}).get("buckets") or []
    if not prev_buckets or len(prev_buckets) != len(cur_buckets):
        return None
    total = _counter_delta((prev or {}).get("count"), (cur or {}).get("count"))
    if total is None or total <= 0:
        return None
    bounds: list[float | None] = []
    cumulative: list[float] = []
    for old, new in zip(prev_buckets, cur_buckets, strict=True):
        old_bound, old_count = old
        new_bound, new_count = new
        if old_bound != new_bound:
            return None
        delta = _counter_delta(old_count, new_count)
        if delta is None:
            return None
        bounds.append(_number(new_bound))  # None 表示 +Inf
        cumulative.append(delta)
    return {"bounds": bounds, "cumulative": cumulative, "total": total}


def _merge_hist(target: dict | None, incoming: dict | None) -> dict | None:
    if incoming is None:
        return target
    if target is None:
        return {
            "bounds": list(incoming["bounds"]),
            "cumulative": list(incoming["cumulative"]),
            "total": incoming["total"],
        }
    if target["bounds"] != incoming["bounds"]:
        return target
    target["total"] += incoming["total"]
    for i, value in enumerate(incoming["cumulative"]):
        target["cumulative"][i] += value
    return target


def _scale_hist(distribution: dict | None, factor: float) -> dict | None:
    if distribution is None or factor >= 1:
        return distribution
    return {
        "bounds": list(distribution["bounds"]),
        "cumulative": [value * factor for value in distribution["cumulative"]],
        "total": distribution["total"] * factor,
    }


def _hist_quantile(distribution: dict | None, q: float) -> float | None:
    if not distribution or distribution["total"] <= 0:
        return None
    target = q * distribution["total"]
    accumulated = 0.0
    lower = 0.0
    previous_cumulative = 0.0
    for bound, cumulative in zip(
        distribution["bounds"], distribution["cumulative"], strict=True
    ):
        count = cumulative - previous_cumulative
        if accumulated + count >= target:
            if count <= 0:
                return lower
            if bound is None:
                return lower
            fraction = (target - accumulated) / count
            return lower + fraction * (bound - lower)
        accumulated += count
        previous_cumulative = cumulative
        if bound is not None:
            lower = bound
    return None


def _hist_quantile_ms(distribution: dict | None, q: float) -> float | None:
    """直方图分位数（秒）转毫秒展示值；无样本返回 None。"""
    value = _hist_quantile(distribution, q)
    return _round1(value * 1000) if value is not None else None


def _interval(prev: InferenceSample, cur: InferenceSample) -> dict:
    prev_data = prev.data or {}
    cur_data = cur.data or {}
    # 有效采样间隔 ≥ LLM_STATS_INTERVAL（默认 5s）；0.1s 下限只用于防御异常
    # 快照，避免 1e-6 把微小间隔放大成百万级 tok/s 的虚假峰值。
    duration = max(cur.ts - prev.ts, 0.1)
    decode = _counter_delta(
        prev_data.get("generation_tokens_total"),
        cur_data.get("generation_tokens_total"),
    )
    prefill = _counter_delta(
        prev_data.get("prompt_tokens_total"), cur_data.get("prompt_tokens_total")
    )
    requests = _counter_delta(
        prev_data.get("request_success_total"), cur_data.get("request_success_total")
    )
    return {
        "ts": cur.ts,
        "task_id": cur.task_id,
        "node_id": cur.node_id,
        "duration": duration,
        "decode": decode,
        "prefill": prefill,
        "requests": requests,
        # 峰值必须使用原始区间速率比较；只在组装响应时舍入展示精度。
        "decode_rate": decode / duration if decode is not None else None,
        "prefill_rate": prefill / duration if prefill is not None else None,
        "request_rate": requests / duration if requests is not None else None,
        "kv_cache_percent": _number(cur_data.get("kv_cache_percent")),
        "ttft": _hist_delta(prev_data.get("ttft"), cur_data.get("ttft")),
        "e2e": _hist_delta(prev_data.get("e2e"), cur_data.get("e2e")),
        "tpot": _hist_delta(prev_data.get("tpot"), cur_data.get("tpot")),
    }


def aggregate_inference_samples(
    rows: Iterable[InferenceSample],
    *,
    from_ts: float,
    to_ts: float,
    max_points: int,
    task_names: dict[int, str],
) -> dict:
    """聚合有序或无序累计快照，返回有界图表点与无降采样窗口摘要。"""
    grouped: dict[tuple[int, int], list[InferenceSample]] = defaultdict(list)
    for row in rows:
        grouped[(row.task_id, row.node_id)].append(row)
    for series_rows in grouped.values():
        series_rows.sort(key=lambda row: (row.ts, row.id))

    window_seconds = max(to_ts - from_ts, 1e-6)
    bucket_seconds = max(window_seconds / max_points, 1e-6)
    buckets: dict[tuple[int, int, int], dict] = {}
    source_intervals = 0
    decode_total = prefill_total = request_total = 0.0
    decode_observed = prefill_observed = False
    decode_peak = prefill_peak = request_peak = None
    decode_peak_at = prefill_peak_at = None
    kv_cache_peak = None
    window_ttft = None

    for (task_id, node_id), series_rows in grouped.items():
        for prev, cur in pairwise(series_rows):
            if cur.ts <= from_ts or cur.ts > to_ts:
                continue
            if cur.ts <= prev.ts:
                # 无效区间：同刻/倒流的累计快照（双 worker 或计数器回落后），
                # 跳过，避免把零时长放大成天文速率峰值
                continue
            item = _interval(prev, cur)
            # 窗口边界通常落在两个采样点之间。按重叠时长线性分摊首个区间，
            # 避免整段计入或整段丢弃；误差上限被限制在一个采样周期内。
            overlap_duration = cur.ts - max(prev.ts, from_ts)
            if overlap_duration <= 0:
                continue
            overlap_factor = min(1.0, overlap_duration / item["duration"])
            if overlap_factor < 1:
                for key in ("decode", "prefill", "requests"):
                    if item[key] is not None:
                        item[key] *= overlap_factor
                item["ttft"] = _scale_hist(item["ttft"], overlap_factor)
                item["e2e"] = _scale_hist(item["e2e"], overlap_factor)
                item["tpot"] = _scale_hist(item["tpot"], overlap_factor)
                item["duration"] = overlap_duration
            source_intervals += 1

            decode = item["decode"]
            prefill = item["prefill"]
            requests = item["requests"]
            if decode is not None:
                decode_observed = True
                decode_total += decode
                if decode_peak is None or item["decode_rate"] > decode_peak:
                    decode_peak = item["decode_rate"]
                    decode_peak_at = item["ts"]
            if prefill is not None:
                prefill_observed = True
                prefill_total += prefill
                if prefill_peak is None or item["prefill_rate"] > prefill_peak:
                    prefill_peak = item["prefill_rate"]
                    prefill_peak_at = item["ts"]
            if requests is not None:
                request_total += requests
                if request_peak is None or item["request_rate"] > request_peak:
                    request_peak = item["request_rate"]
            if item["kv_cache_percent"] is not None and (
                kv_cache_peak is None or item["kv_cache_percent"] > kv_cache_peak
            ):
                kv_cache_peak = item["kv_cache_percent"]
            window_ttft = _merge_hist(window_ttft, item["ttft"])

            bucket_index = min(
                max_points - 1,
                max(0, int((item["ts"] - from_ts) / bucket_seconds)),
            )
            key = (task_id, node_id, bucket_index)
            bucket = buckets.setdefault(
                key,
                {
                    "task_id": task_id,
                    "task_name": task_names.get(task_id),
                    "decode": 0.0,
                    "prefill": 0.0,
                    "requests": 0.0,
                    "decode_duration": 0.0,
                    "prefill_duration": 0.0,
                    "kv_cache_percent": None,
                    "ttft": None,
                    "e2e": None,
                    "tpot": None,
                },
            )
            if decode is not None:
                bucket["decode"] += decode
                bucket["decode_duration"] += item["duration"]
            if prefill is not None:
                bucket["prefill"] += prefill
                bucket["prefill_duration"] += item["duration"]
            if requests is not None:
                bucket["requests"] += requests
            if item["kv_cache_percent"] is not None:
                current = bucket["kv_cache_percent"]
                bucket["kv_cache_percent"] = (
                    item["kv_cache_percent"]
                    if current is None
                    else max(current, item["kv_cache_percent"])
                )
            bucket["ttft"] = _merge_hist(bucket["ttft"], item["ttft"])
            bucket["e2e"] = _merge_hist(bucket["e2e"], item["e2e"])
            bucket["tpot"] = _merge_hist(bucket["tpot"], item["tpot"])

    points = []
    for key, bucket in sorted(
        buckets.items(), key=lambda item: (item[0][2], item[0][0], item[0][1])
    ):
        bucket_index = key[2]
        decode_duration = bucket.pop("decode_duration")
        prefill_duration = bucket.pop("prefill_duration")
        ttft = bucket.pop("ttft")
        e2e = bucket.pop("e2e")
        tpot = bucket.pop("tpot")
        decode = bucket.pop("decode")
        prefill = bucket.pop("prefill")
        point = {
            **bucket,
            "ts": min(to_ts, from_ts + (bucket_index + 1) * bucket_seconds),
            "tokens_per_sec": _round1(decode / decode_duration)
            if decode_duration
            else None,
            "prompt_tokens_per_sec": (
                _round1(prefill / prefill_duration) if prefill_duration else None
            ),
            # 桶内原始 token 体量（不经过除法），供输入/输出体量图使用
            "generated_tokens": round(decode),
            "prompt_tokens": round(prefill),
            "requests": round(bucket["requests"]),
            "ttft_p50_ms": _hist_quantile_ms(ttft, 0.50),
            "ttft_p95_ms": _hist_quantile_ms(ttft, 0.95),
            "e2e_p50_ms": _hist_quantile_ms(e2e, 0.50),
            "e2e_p95_ms": _hist_quantile_ms(e2e, 0.95),
            "tpot_p50_ms": _hist_quantile_ms(tpot, 0.50),
            "tpot_p95_ms": _hist_quantile_ms(tpot, 0.95),
        }
        points.append(point)

    ttft_p95 = _hist_quantile(window_ttft, 0.95)
    return {
        "bucket_seconds": bucket_seconds,
        "source_intervals": source_intervals,
        "points": points,
        "summary": {
            "decode_average_tokens_per_sec": (
                _round1(decode_total / window_seconds) if decode_observed else None
            ),
            "prefill_average_tokens_per_sec": (
                _round1(prefill_total / window_seconds) if prefill_observed else None
            ),
            "decode_peak_tokens_per_sec": _round1(decode_peak)
            if decode_peak is not None
            else None,
            "decode_peak_at": decode_peak_at,
            "prefill_peak_tokens_per_sec": (
                _round1(prefill_peak) if prefill_peak is not None else None
            ),
            "prefill_peak_at": prefill_peak_at,
            "request_peak_per_sec": _round1(request_peak)
            if request_peak is not None
            else None,
            "ttft_p95_ms": _round1(ttft_p95 * 1000) if ttft_p95 is not None else None,
            "kv_cache_peak_percent": round(kv_cache_peak, 3)
            if kv_cache_peak is not None
            else None,
            "window_generated_tokens": round(decode_total),
            "window_prompt_tokens": round(prefill_total),
            "window_requests": round(request_total),
        },
    }
