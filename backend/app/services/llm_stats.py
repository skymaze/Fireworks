"""LLM 推理统计：对运行中含 VLLM_PORT 的推理任务（vLLM 等）周期性落**原始累计快照**。

- `service_endpoint(task)`：复用 head + VLLM_PORT 发现逻辑（tasks.py 健康检查同源），
  返回推理服务端点（head 节点、url_base、model）；非 vLLM 类任务返回 None。
- `stats_task_loop()`：每 LLM_STATS_INTERVAL 扫描 running 任务 -> head agent 读取
  vLLM /metrics 原始累计快照（计数器 / KV gauge / 直方图 sum+count+buckets，无状态）-> 落
  InferenceSample。活跃期逐点保留；空闲期滚动刷新一个边界点，既保证下一次流量
  的时间分母准确，也避免无流量时持续膨胀。
- 差分与时间桶聚合由查询服务完成，不广播。
- 默认保留 25h，为完整 24h 窗口保留窗口前差分基线。
"""

import asyncio
import logging
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import config
from ..db import SessionLocal
from ..models import InferenceSample, Node, Task
from . import agent_client, agent_ws, task_runtime

logger = logging.getLogger(__name__)

_ACTIVITY_FIELDS = (
    "generation_tokens_total",
    "prompt_tokens_total",
    "request_success_total",
    # KV 是瞬时 gauge；请求执行期间可能先变化，而累计计数器到请求结束才结算。
    # 将它纳入空闲判定，避免原地滚动快照时覆盖掉请求中的 KV 峰值。
    "kv_cache_percent",
)


def _same_activity(left: dict | None, right: dict | None) -> bool:
    return all(
        (left or {}).get(key) == (right or {}).get(key) for key in _ACTIVITY_FIELDS
    )


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


async def _store_snapshot(db: Session, task: Task, head: Node, snapshot: dict) -> None:
    """原始累计快照落库（InferenceSample）。

    活跃期每轮落点。无流量时保留两个相同活动状态端点，并滚动刷新最新端点时间；
    下一次流量会以紧邻采集周期前的空闲端点为基线，同时长空闲期只占一个额外行。
    活动状态包含累计计数器和 KV gauge，因此请求中的瞬时 KV 峰值不会被覆盖；
    计数器回退由聚合层视为新基线。
    """
    # Agent 请求期间任务可能被用户删除。写入前取得数据库写锁并复查状态，保证
    # “复查 + 插入”与任务删除串行，SQLite 关闭外键时也不会产生晚到孤儿记录。
    fresh_task = task_runtime.lock_task_for_write(db, task.id, {"running"})
    if fresh_task is None:
        db.rollback()
        return
    data = {
        "generation_tokens_total": snapshot.get("generation_tokens_total"),
        "prompt_tokens_total": snapshot.get("prompt_tokens_total"),
        "request_success_total": snapshot.get("request_success_total"),
        "kv_cache_percent": snapshot.get("kv_cache_percent"),
        "ttft": snapshot.get("ttft"),
        "e2e": snapshot.get("e2e"),
    }
    previous = (
        db.query(InferenceSample)
        .filter(
            InferenceSample.task_id == fresh_task.id, InferenceSample.node_id == head.id
        )
        .order_by(InferenceSample.ts.desc())
        .limit(2)
        .all()
    )
    now = time.time()
    if (
        previous
        and _same_activity(previous[0].data, data)
        and len(previous) >= 2
        and _same_activity(previous[0].data, previous[1].data)
    ):
        # 已有两个相同端点说明最新行就是空闲边界：原地滚动到本次轮询时刻。
        previous[0].ts = now
        previous[0].data = data
        db.commit()
        return
    # 首次发现空闲仍插入一个端点，不能移动刚刚承载真实流量的样本时间。
    db.add(
        InferenceSample(
            task_id=fresh_task.id,
            node_id=head.id,
            ts=now,
            data=data,
        )
    )
    db.commit()


def ensure_inference_indexes(db: Session) -> None:
    """为升级自旧库的部署补建 24h 范围与序列基线查询索引。"""
    db.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_inference_task_node_ts "
            "ON inference_samples (task_id, node_id, ts)"
        )
    )
    db.commit()


async def stats_once() -> None:
    """单轮统计：扫描 running 任务，经 head agent 读取原始累计快照并落库。

    活跃期逐点落库，空闲期滚动维护边界快照；差分与聚合由查询服务完成。
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
            head, url_base, _ = endpoint
            # head WS 不在线则跳过（避免每轮对不可达节点发起 5s 连接超时探测）
            if not agent_ws.is_connected(head.id):
                continue
            try:
                result = await agent_client.inference_stats(
                    head,
                    {
                        "url_base": url_base,
                        "timeout": min(config.LLM_STATS_INTERVAL + 5, 10),
                    },
                )
            except Exception as e:
                # 统计失败属常态（容器 warming/瞬时不可达），低频记录即可
                logger.debug("task %d LLM 统计失败: %s", task.id, e)
                continue
            if not result.get("ok"):
                continue
            # 无计数器（非 vLLM 后端 / metrics 不可用）：不产数据
            if result.get("generation_tokens_total") is None:
                continue
            try:
                await _store_snapshot(db, task, head, result)
            except Exception:
                logger.exception("task %d 推理统计入库失败", task.id)
                # 复用同一会话：入库异常后刷新，避免脏会话污染后续任务
                db.rollback()
    finally:
        db.close()


def cleanup_legacy_inference_samples(db: Session) -> int:
    """升级清理：删除旧格式的推理统计样本（缺少新累计计数器键的行）。

    旧版 data 为派生格式（tokens_per_sec/ttft_ms/...），新格式必含
    generation_tokens_total；用 json_type 按"键是否存在"判定（兼容"键存在但值为
    null"的新行，不误删）。幂等——升级跑一次后即为空操作。
    """
    n = (
        db.execute(
            text(
                "DELETE FROM inference_samples "
                "WHERE json_type(data, '$.generation_tokens_total') IS NULL"
            )
        ).rowcount
        or 0
    )
    db.commit()
    if n:
        logger.info("启动数据清理：删除 %d 条旧格式推理统计样本", n)
    return n


async def stats_task_loop() -> None:
    while True:
        try:
            await stats_once()
        except Exception:
            logger.exception("LLM 推理统计轮询失败")
        await asyncio.sleep(config.LLM_STATS_INTERVAL)
