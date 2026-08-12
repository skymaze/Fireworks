"""LLM 推理统计：对运行中含 VLLM_PORT 的推理任务（vLLM 等）周期性落**原始累计快照**。

- `service_endpoint(task)`：复用 head + VLLM_PORT 发现逻辑（tasks.py 健康检查同源），
  返回推理服务端点（head 节点、url_base、model）；非 vLLM 类任务返回 None。
- `stats_task_loop()`：每 LLM_STATS_INTERVAL 扫描 running 任务 -> head agent 读取
  vLLM /metrics 原始累计快照（计数器 / KV gauge / 直方图 sum+count+buckets，无状态）-> 落
  InferenceSample；仅当与上一份相比有真实流量时才落点（无请求即无数据）。
- 差分（tok/s、TTFT/E2E、增量）与统计/绘图由前端拉取原始数据完成，后端不派生、不广播。
- 与 metrics.py 同一保留期（24h），清理并入 metrics_loop 的节流清理。
"""

import asyncio
import logging
import time

from sqlalchemy.orm import Session

from .. import config
from ..db import SessionLocal
from ..models import InferenceSample, Node, Task
from . import agent_client, agent_ws, task_runtime

logger = logging.getLogger(__name__)


def service_endpoint(db: Session, task: Task) -> tuple[Node, str, str | None] | None:
    """从任务渲染结果推导推理服务端点：(head_node, url_base, model)。

    仅当 head 渲染 env 含 VLLM_PORT（vLLM 类服务）时返回；url_base 走容器
    host 网络直连（seed 配方 network_mode=host）+ 127.0.0.1 环回。
    """
    rendered = task.rendered or {}
    for node_id, payload in (rendered.get("nodes") or {}).items():
        if payload.get("role") != "head":
            continue
        env = payload.get("env") or {}
        port = env.get("VLLM_PORT")
        if not port:
            return None
        head = db.get(Node, int(node_id))
        if head is None:
            return None
        model = (
            env.get("SERVED_MODEL_NAME")
            or env.get("MODEL_ID")
            or env.get("DSPARK_MODEL")
        )
        return head, f"http://127.0.0.1:{port}", model
    return None


# 原始快照中用于"是否有真实流量"判定的累计计数器（四者全 0 视为无变化）
_SNAPSHOT_COUNTERS = (
    "generation_tokens_total",
    "prompt_tokens_total",
    "num_preemptions_total",
    "request_success_total",
)


def _snapshot_delta(prev_data: dict | None, snapshot: dict) -> list[float | None]:
    """相邻两份快照的四类计数器增量；prev 缺失或新旧格式不匹配返回 None 元素。"""
    deltas: list[float | None] = []
    for key in _SNAPSHOT_COUNTERS:
        p = (prev_data or {}).get(key)
        c = snapshot.get(key)
        deltas.append(None if p is None or c is None else c - p)
    return deltas


async def _store_snapshot(
    db: Session, task: Task, head: Node, model: str | None, snapshot: dict
) -> None:
    """原始累计快照落库（InferenceSample）。

    仅当与上一份已存样本相比有真实流量（四类计数器任一增量非 0）时落点；
    计数器无变化则不落点（"无请求即无数据"）。新旧格式不匹配或负增量
    （vLLM 重启计数清零）也会落点，作为新的差分基线。
    """
    # Agent 请求期间任务可能被用户删除。写入前取得数据库写锁并复查状态，保证
    # “复查 + 插入”与任务删除串行，SQLite 关闭外键时也不会产生晚到孤儿记录。
    fresh_task = task_runtime.lock_task_for_write(db, task.id, {"running"})
    if fresh_task is None:
        db.rollback()
        return
    prev = (
        db.query(InferenceSample)
        .filter(InferenceSample.task_id == task.id)
        .order_by(InferenceSample.ts.desc())
        .first()
    )
    if prev is not None:
        deltas = _snapshot_delta(prev.data, snapshot)
        if all(d is not None and d == 0 for d in deltas):
            db.rollback()  # 已建基线且无变化：无真实流量，不落点
            return
    data = {
        "backend": snapshot.get("backend", "unknown"),
        "generation_tokens_total": snapshot.get("generation_tokens_total"),
        "prompt_tokens_total": snapshot.get("prompt_tokens_total"),
        "num_preemptions_total": snapshot.get("num_preemptions_total"),
        "request_success_total": snapshot.get("request_success_total"),
        "kv_cache_percent": snapshot.get("kv_cache_percent"),
        "ttft": snapshot.get("ttft"),
        "e2e": snapshot.get("e2e"),
    }
    db.add(InferenceSample(
        task_id=fresh_task.id,
        node_id=head.id,
        ts=time.time(),
        model_name=model,
        data=data,
    ))
    db.commit()


async def stats_once() -> None:
    """单轮统计：扫描 running 任务，经 head agent 读取原始累计快照并落库。

    仅当与上一份已存样本相比有真实流量时落点（见 _store_snapshot）；
    无请求即无数据。落库均为原始累计数据，差分与绘图由前端完成。
    """
    if not config.LLM_STATS_ENABLED:
        return
    db = SessionLocal()
    try:
        tasks = db.query(Task).filter(Task.status == "running").all()
        for task in tasks:
            endpoint = service_endpoint(db, task)
            if endpoint is None:
                continue  # 非推理类任务 / 无 VLLM_PORT
            head, url_base, model = endpoint
            # head WS 不在线则跳过（避免每轮对不可达节点发起 5s 连接超时探测）
            if not agent_ws.is_connected(head.id):
                continue
            try:
                result = await agent_client.inference_stats(head, {
                    "url_base": url_base,
                    "model": model or "default",
                    "timeout": min(config.LLM_STATS_INTERVAL + 5, 10),
                })
            except Exception as e:  # noqa: BLE001
                # 统计失败属常态（容器 warming/瞬时不可达），低频记录即可
                logger.debug("task %d LLM 统计失败: %s", task.id, e)
                continue
            if not result.get("ok"):
                continue
            # 无计数器（非 vLLM 后端 / metrics 不可用）：不产数据
            if result.get("generation_tokens_total") is None:
                continue
            try:
                await _store_snapshot(db, task, head, model, result)
            except Exception:  # noqa: BLE001
                logger.exception("task %d 推理统计入库失败", task.id)
                # 复用同一会话：入库异常后刷新，避免脏会话污染后续任务
                db.rollback()
    finally:
        db.close()


async def stats_task_loop() -> None:
    while True:
        try:
            await stats_once()
        except Exception:  # noqa: BLE001
            logger.exception("LLM 推理统计轮询失败")
        await asyncio.sleep(config.LLM_STATS_INTERVAL)
