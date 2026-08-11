"""LLM 探针：对运行中含 VLLM_PORT 的推理任务（vLLM 等）周期性探测实时推理指标。

- `service_endpoint(task)`：复用 head + VLLM_PORT 发现逻辑（tasks.py 健康检查同源），
  返回推理服务端点（head 节点、url_base、model）；非 vLLM 类任务返回 None。
- `probe_task_loop()`：每 LLM_PROBE_INTERVAL 扫描 running 任务 -> head agent 探测 ->
  写 InferenceSample + 广播 `inference_metrics`（前端任务页实时曲线）。
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


async def _store_and_broadcast(
    db: Session, task: Task, head: Node, model: str | None, result: dict
) -> None:
    """探针结果入库（InferenceSample）+ 广播 inference_metrics（前端实时曲线）。"""
    # Agent 请求期间任务可能被用户删除。写入前取得数据库写锁并复查状态，保证
    # “复查 + 插入”与任务删除串行，SQLite 关闭外键时也不会产生晚到孤儿记录。
    fresh_task = task_runtime.lock_task_for_write(db, task.id, {"running"})
    if fresh_task is None:
        db.rollback()
        return
    data = {
        "backend": result.get("backend", "unknown"),
        "tokens_per_sec": result.get("tokens_per_sec"),
        "ttft_ms": result.get("ttft_ms"),
        "e2e_ms": result.get("e2e_ms"),
        "itl_p50_ms": result.get("itl_p50_ms"),
        "itl_p95_ms": result.get("itl_p95_ms"),
        "kv_cache_percent": result.get("kv_cache_percent"),
        "preemptions": result.get("preemptions"),
        "output_tokens": result.get("output_tokens"),
        "prompt_tokens": result.get("prompt_tokens"),
        "ts": time.time(),
    }
    db.add(InferenceSample(
        task_id=fresh_task.id,
        node_id=head.id,
        ts=time.time(),
        model_name=model,
        data=data,
    ))
    task_id = fresh_task.id
    task_name = fresh_task.name
    task_status = fresh_task.status
    db.commit()
    await agent_ws.broadcast({
        "type": "inference_metrics",
        "task_id": task_id,
        "task_name": task_name,
        "task_status": task_status,
        "model_name": model,
        "node_id": head.id,
        "data": data,
    })


async def probe_once() -> None:
    """单轮探针：扫描 running 任务，对其推理端点经 head agent 探测并入库广播。"""
    if not config.LLM_PROBE_ENABLED:
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
                result = await agent_client.llm_probe(head, {
                    "url_base": url_base,
                    "model": model or "default",
                    "max_tokens": config.LLM_PROBE_MAX_TOKENS,
                    "timeout": min(config.LLM_PROBE_INTERVAL + 5, 10),
                })
            except Exception as e:  # noqa: BLE001
                # 探针失败属常态（容器warming/瞬时不可达），低频记录即可
                logger.debug("task %d LLM 探针失败: %s", task.id, e)
                continue
            if not result.get("ok"):
                continue
            try:
                await _store_and_broadcast(db, task, head, model, result)
            except Exception:  # noqa: BLE001
                logger.exception("task %d 探针结果入库失败", task.id)
                # 复用同一会话：入库异常后刷新，避免脏会话污染后续任务
                db.rollback()
    finally:
        db.close()


async def probe_task_loop() -> None:
    while True:
        try:
            await probe_once()
        except Exception:  # noqa: BLE001
            logger.exception("LLM 探针轮询失败")
        await asyncio.sleep(config.LLM_PROBE_INTERVAL)
