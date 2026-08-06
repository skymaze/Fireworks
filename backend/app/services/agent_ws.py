"""后端 -> Agent WebSocket 连接管理：实时接收指标/容器事件/日志流/传输进度。

- 每个节点一条 WS 连接（后端为客户端），断连指数退避重连（1s→60s）；
- 消息分发：
  metrics        -> 写 MetricSample 入库 + 广播前端（节点状态实时）
  docker_event   -> 实时更新 TaskNode.container_status + 触发任务 stopped（秒级）
  log / log_end  -> 转发给订阅该容器的前端
  progress       -> 更新 ModelDownload/ImageTransfer.sent_bytes + 广播前端
- 前端广播：每个前端连接一个队列 + 发送任务，broadcast 投递；
  日志消息只投递给订阅者。
- HTTP 轮询（metrics.py / task_monitor.py）保留为兜底：WS 健康节点跳过轮询。
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import websockets

from .. import background_tasks
from ..db import SessionLocal
from ..models import ImageTransfer, MetricSample, ModelDownload, Node, Task, TaskNode
from ..security import get_agent_token

logger = logging.getLogger(__name__)

MAX_BACKOFF = 60
SYNC_INTERVAL = 30  # 节点表变化同步周期（新增节点自动连）
# 单前端连接最多订阅的容器日志流数（每条流=节点上一个 `docker logs -f` 子进程，
# 防止恶意/失控页面在一个 WS 连接上开无限流打满节点进程数）
MAX_LOG_SUBS_PER_CLIENT = 50

# node_id -> 连接任务（含重连循环）
_conn_tasks: dict[int, asyncio.Task] = {}
# node_id -> WS 健康状态（metrics.py/task_monitor 据此跳过轮询）
_connected: dict[int, bool] = {}
# 前端连接注册表：{前端队列: 订阅的容器集合}
_frontend_queues: dict[asyncio.Queue, set[str]] = {}
# 容器名 -> 订阅该容器的前端队列集合（agent 侧同一容器只开一条日志流）
_log_subscribers: dict[str, set[asyncio.Queue]] = {}
# (node_id, container) -> 应保持的 agent 日志流需求：
# 登记订阅即加入；agent WS 断连重连后据此补发 log_subscribe（agent 侧流随断连终止）
_agent_log_subs: set[tuple[int, str]] = set()

_stop = asyncio.Event()


def is_connected(node_id: int) -> bool:
    """该节点 WS 是否健康（metrics/task_monitor 据此跳过 HTTP 轮询）。"""
    return _connected.get(node_id, False)


# ---------- 前端广播 ----------


async def _send(ws, msg: dict) -> None:
    try:
        # websockets>=12 的 ClientConnection 无 send_json，统一 send 文本帧
        await ws.send(json.dumps(msg, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        logger.warning("WS 发送失败: %s (%s)", type(e).__name__, e)


# 广播丢帧统计（前端消费满时低频标记，不静默丢弃）
_dropped_frames = 0
_last_drop_marker = 0.0


async def broadcast(msg: dict, exclude: asyncio.Queue | None = None) -> None:
    """广播给所有前端连接（日志消息只投递给订阅者）。"""
    global _dropped_frames, _last_drop_marker
    is_log = msg.get("type") in ("log", "log_end")
    if is_log:
        container = msg.get("container", "")
        subs = _log_subscribers.get(container)
        if not subs:
            return
        targets = list(subs)
    else:
        targets = list(_frontend_queues.keys())
    dropped = False
    for q in targets:
        if q is exclude:
            continue
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dropped = True
    if dropped:
        _dropped_frames += 1
        now = time.monotonic()
        # 低频丢帧标记：慢消费者可感知数据缺口，而非无形丢数据
        if now - _last_drop_marker >= 10:
            _last_drop_marker = now
            logger.warning(
                "WS 广播累计丢帧 %d：存在消费端队列满载（前端过慢/订阅过多）",
                _dropped_frames,
            )
            marker = {"type": "dropped_frames", "count": _dropped_frames}
            for q in list(_frontend_queues):
                if q is exclude:
                    continue
                try:
                    q.put_nowait(marker)
                except asyncio.QueueFull:
                    pass


def register_frontend(q: asyncio.Queue) -> None:
    _frontend_queues[q] = set()


def unregister_frontend(q: asyncio.Queue) -> None:
    """前端断开：清理其日志订阅；容器无订阅者时向 agent 退订日志流。"""
    _frontend_queues.pop(q, None)
    containers = {c for c, subs in _log_subscribers.items() if q in subs}
    for c in containers:
        _log_subscribers[c].discard(q)
        if not _log_subscribers[c]:
            _log_subscribers.pop(c, None)
            # 容器名全局唯一（每任务每节点一个容器），可反查所属节点
            db = SessionLocal()
            try:
                tn = (db.query(TaskNode)
                      .filter(TaskNode.container_name == c).first())
                node_id = tn.node_id if tn else None
            finally:
                db.close()
            if node_id is not None:
                _agent_log_subs.discard((node_id, c))
                asyncio.create_task(_agent_send_cmd(c, "log_unsubscribe", node_id=node_id))


async def subscribe_log(node_id: int, container: str, q: asyncio.Queue,
                        tail: int = 0) -> None:
    """前端订阅容器日志：注册转发目标；agent 未开流时下发订阅命令。

    tail=0：agent 日志流只推送订阅后的新行，历史快照由前端 HTTP 拉取，
    避免 `docker logs -f --tail N` 回放与快照重叠导致重复行。
    """
    if not container:
        return
    if len(_frontend_queues.get(q, set())) >= MAX_LOG_SUBS_PER_CLIENT:
        logger.warning("前端连接订阅容器数达上限 %d，拒绝订阅 %s", MAX_LOG_SUBS_PER_CLIENT, container)
        return
    _frontend_queues.setdefault(q, set()).add(container)
    _log_subscribers.setdefault(container, set()).add(q)
    key = (node_id, container)
    first = key not in _agent_log_subs
    _agent_log_subs.add(key)  # 登记需求：断连重连后据此补发
    if first and is_connected(node_id):
        await _agent_send_cmd(container, "log_subscribe", node_id=node_id, tail=tail)


async def unsubscribe_log(node_id: int, container: str, q: asyncio.Queue) -> None:
    subs = _log_subscribers.get(container)
    if subs:
        subs.discard(q)
        if not subs:
            _log_subscribers.pop(container, None)
            _agent_log_subs.discard((node_id, container))
            await _agent_send_cmd(container, "log_unsubscribe", node_id=node_id)
    _frontend_queues.get(q, set()).discard(container)


# ---------- Agent 连接管理 ----------


async def _agent_send_cmd(container: str, cmd: str, node_id: int | None = None,
                          tail: int | None = None) -> None:
    """向某容器所在节点的 agent 发送订阅控制命令（经对应 WS 连接）。"""
    # 从连接注册表反查节点：容器 -> TaskNode -> node_id
    if node_id is None:
        db = SessionLocal()
        try:
            tn = (db.query(TaskNode).filter(TaskNode.container_name == container).first())
            node_id = tn.node_id if tn else None
        finally:
            db.close()
    if node_id is None:
        return
    conn = _conn_tasks.get(node_id)
    if conn is None:
        return
    # 连接任务内部维护的 ws 对象
    ws = getattr(conn, "_ws", None)
    if ws is None:
        return
    msg = {"type": cmd, "container": container}
    if tail is not None:
        msg["tail"] = tail
    await _send(ws, msg)


async def _handle_message(node: Node, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "metrics":
        await _on_metrics(node, msg.get("data") or {})
    elif mtype == "docker_event":
        await _on_docker_event(node, msg.get("data") or {})
    elif mtype in ("log", "log_end"):
        await broadcast(msg)
    elif mtype == "progress":
        await _on_progress(node, msg)


async def _on_metrics(node: Node, data: dict) -> None:
    now = time.time()
    db = SessionLocal()
    try:
        n = db.get(Node, node.id)
        if n is None:
            return
        n.agent_status = "online"
        n.last_seen = datetime.now(timezone.utc)
        db.add(MetricSample(node_id=node.id, ts=data.get("ts", now), data=data))
        db.commit()
    finally:
        db.close()
    await broadcast({"type": "metrics", "node_id": node.id, "data": data})


_DOCKER_STATE = {
    "start": "running", "die": "exited", "stop": "exited",
    "pause": "paused", "unpause": "running",
}


async def _on_docker_event(node: Node, ev: dict) -> None:
    status = ev.get("status")
    new_state = _DOCKER_STATE.get(status)
    attrs = (ev.get("Actor") or {}).get("Attributes") or {}
    name = attrs.get("name")
    if not new_state or not name:
        return
    db = SessionLocal()
    try:
        tn = db.query(TaskNode).filter(TaskNode.container_name == name).first()
        if not tn:
            return
        tn.container_status = new_state
        db.commit()
        task = db.get(Task, tn.task_id)
        # 容器全退出 -> 任务 stopped（秒级；仅当任务仍为 running，不覆盖用户操作）
        if task and task.status == "running" and status in ("die", "stop"):
            states = [x.container_status for x in task.nodes]
            if states and len(states) == len(list(task.nodes)) and all(
                s == "exited" for s in states
            ):
                task.status = "stopped"
                db.commit()
                await broadcast({"type": "task_status", "task_id": task.id,
                                 "status": "stopped"})
        await broadcast({"type": "container_status", "task_id": tn.task_id,
                         "node_id": tn.node_id, "container": name,
                         "status": new_state})
    finally:
        db.close()


async def _on_progress(node: Node, msg: dict) -> None:
    """agent 拉取进度 -> 更新传输任务 sent_bytes（head 拉取即发送阶段）+ 广播。"""
    kind = msg.get("kind")
    key = msg.get("key", "")
    written = msg.get("written") or 0
    db = SessionLocal()
    try:
        if kind == "model":
            job = (db.query(ModelDownload)
                   .filter(ModelDownload.repo == key,
                           ModelDownload.status.in_(
                               ["downloading", "sending", "syncing", "paused"]))
                   .order_by(ModelDownload.id.desc()).first())
            if job and job.head_node_id == node.id:
                job.sent_bytes = written
                db.commit()
                await broadcast({"type": "transfer_progress", "kind": "model",
                                 "job_id": job.id, "sent_bytes": written,
                                 "total_bytes": job.total_bytes})
        elif kind == "image":
            t = (db.query(ImageTransfer)
                 .filter(ImageTransfer.digest == key,
                         ImageTransfer.status.in_(
                             ["pulling", "sending", "syncing", "loading", "paused"]))
                 .order_by(ImageTransfer.id.desc()).first())
            if t and t.head_node_id == node.id:
                t.sent_bytes = written
                db.commit()
                await broadcast({"type": "transfer_progress", "kind": "image",
                                 "job_id": t.id, "sent_bytes": written,
                                 "total_bytes": t.size_bytes})
    finally:
        db.close()


def _ws_additional_headers() -> list[tuple[str, str]]:
    """控制平面 -> Agent WS 握手携带共享 token（后端作为客户端，agent 侧校验）。"""
    return [("Authorization", f"Bearer {get_agent_token()}")]


async def _connect_node(node: Node) -> None:
    """单节点连接循环：连接 -> 收消息分发 -> 断连退避重连。"""
    url = f"ws://{node.ip}:{node.agent_port}/ws/events"
    backoff = 1
    task = asyncio.current_task()
    while not _stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          open_timeout=10, max_size=4 * 1024 * 1024,
                                          additional_headers=_ws_additional_headers()) as ws:
                task._ws = ws  # 供订阅控制命令复用当前连接
                _connected[node.id] = True
                backoff = 1
                logger.info("WS 已连接 agent %s (%s)", node.name, node.ip)
                # agent 侧日志流随断连终止：重连后补发所有仍被订阅的容器
                for nid, container in list(_agent_log_subs):
                    if nid == node.id:
                        await _send(ws, {"type": "log_subscribe",
                                         "container": container, "tail": 0})
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    try:
                        await _handle_message(node, msg)
                    except Exception:  # noqa: BLE001
                        logger.exception("agent %s WS 消息处理失败", node.name)
                task._ws = None
                _connected[node.id] = False
                logger.warning("WS 断开 agent %s，%.0fs 后重连", node.name, backoff)
        except asyncio.CancelledError:
            task._ws = None
            _connected[node.id] = False
            raise
        except Exception as e:  # noqa: BLE001
            task._ws = None
            _connected[node.id] = False
            logger.warning("WS 连接 agent %s 失败: %s（%.0fs 后重试）", node.name, e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


async def _sync_connections() -> None:
    """周期同步：新节点自动建立连接；死任务清理。"""
    while not _stop.is_set():
        try:
            db = SessionLocal()
            try:
                nodes = db.query(Node).all()
                node_ids = {n.id for n in nodes}
            finally:
                db.close()
            for n in nodes:
                if n.id not in _conn_tasks or _conn_tasks[n.id].done():
                    task = asyncio.create_task(_connect_node(n))
                    _conn_tasks[n.id] = task
            for nid in list(_conn_tasks):
                if nid not in node_ids:
                    _conn_tasks.pop(nid, None).cancel()
        except Exception:  # noqa: BLE001
            logger.exception("WS 连接同步失败")
        await asyncio.sleep(SYNC_INTERVAL)


async def start() -> None:
    _stop.clear()
    background_tasks.spawn(_sync_connections())


async def stop() -> None:
    _stop.set()
    for task in _conn_tasks.values():
        task.cancel()
    _conn_tasks.clear()
    _connected.clear()
