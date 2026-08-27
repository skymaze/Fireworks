"""后端 -> Agent WebSocket 连接管理：实时接收指标/容器事件/日志流/传输进度。

- 每个节点一条 WS 连接（后端为客户端），断连指数退避重连（1s→60s）；
- 消息分发：
  metrics        -> 写 MetricSample 入库 + 广播前端（节点状态实时）
  docker_event   -> 实时更新 TaskNode.container_status + 触发任务 stopped（秒级）
  log / log_end  -> 转发给订阅该容器的前端
  progress       -> 更新 ModelDownload/ImageTransfer.sent_bytes + 广播前端
- 节点存活（agent_status）单一来源 = WS 连接状态 + 心跳看门狗：
  握手成功即置 online，断开/连接失败/心跳超时即置 offline（落库 + 广播 node_status）。
- 前端广播：每个前端连接一个队列 + 发送任务，broadcast 投递；
  日志消息只投递给订阅者。
- HTTP 轮询（metrics.py / task_monitor.py）保留为纯数据兜底：WS 健康节点跳过轮询，
  且不再参与 online/offline 判定。
"""

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone

import websockets

from .. import background_tasks, config
from ..db import SessionLocal
from ..models import ImageTransfer, MetricSample, ModelDownload, Node, Task, TaskNode

logger = logging.getLogger(__name__)

MAX_BACKOFF = 60
SYNC_INTERVAL = 30  # 节点表变化同步周期（新增节点自动连）
# 单前端连接最多订阅的容器日志流数（每条流=节点上一个 `docker logs -f` 子进程，
# 防止恶意/失控页面在一个 WS 连接上开无限流打满节点进程数）
MAX_LOG_SUBS_PER_CLIENT = 50
# 初次订阅和 Agent 重连时由同一条 `docker logs -f` 流回放历史并继续追踪，
# 从协议上消除“HTTP 快照完成后、WS 实时流启动前”的日志空窗。
LOG_REPLAY_TAIL = 1000
MAX_LOG_REPLAY_TAIL = 5000
# 后加入的前端订阅者从控制平面的有界缓存回放，不会只看到订阅后的新行。
LOG_HISTORY_MAX_MESSAGES = 1500
LOG_HISTORY_MAX_BYTES = 2 * 1024 * 1024

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
# (node_id, container) -> 当前日志流代际。退订后保留计数，确保快速重订阅时
# 旧 reader 晚到的 log_end 不会结束新流。
_log_generations: dict[tuple[int, str], int] = {}
# container -> deque[(message, estimated_bytes)]
_log_history: dict[str, deque[tuple[dict, int]]] = {}
_log_history_bytes: dict[str, int] = {}

_stop = asyncio.Event()

# 心跳看门狗周期（秒）：节点 WS 连接存活但超过 NODE_STALE_TIMEOUT 无任何消息
# （agent 每 5s 必推 metrics 即应用级心跳）=> 判定离线并强制重连，避免悬挂连接。
WATCHDOG_INTERVAL = 5
# node_id -> 最近一次收到 agent WS 消息的时间戳（time.time()）
_last_msg_ts: dict[int, float] = {}
# (模型任务, head 节点, 相对路径) -> 当前文件已写字节；用于并发文件的实时聚合。
_model_file_progress: dict[tuple[int, int, str], int] = {}


def clear_model_file_progress(job_id: int) -> None:
    """模型发送阶段结束后释放按文件累计的临时进度。"""
    for key in [key for key in _model_file_progress if key[0] == job_id]:
        _model_file_progress.pop(key, None)


def is_connected(node_id: int) -> bool:
    """该节点 WS 是否健康（metrics/task_monitor 据此跳过 HTTP 轮询）。"""
    return _connected.get(node_id, False)


async def _set_node_status(node_id: int, status: str) -> None:
    """统一节点运行时状态写入（online/offline）：落库 + 广播 node_status 事件。

    状态单一来源 = Agent WS 连接 + 心跳看门狗；metrics.py 等其它模块不再直接
    翻转 agent_status，避免双写竞争。幂等：状态未变不重复落库/广播。
    """
    db = SessionLocal()
    try:
        n = db.get(Node, node_id)
        if n is None:
            return
        if n.agent_status == status:
            return
        n.agent_status = status
        if status == "online":
            n.last_seen = datetime.now(timezone.utc)
        db.commit()
        last_seen = n.last_seen
    finally:
        db.close()
    await broadcast({
        "type": "node_status",
        "node_id": node_id,
        "status": status,
        "last_seen": last_seen.isoformat() if last_seen else None,
    })


# ---------- 前端广播 ----------


async def _send(ws, msg: dict) -> None:
    try:
        # websockets>=12 的 ClientConnection 无 send_json，统一 send 文本帧
        await ws.send(json.dumps(msg, ensure_ascii=False))
    except Exception as e:
        logger.warning("WS 发送失败: %s (%s)", type(e).__name__, e)


# 广播丢帧统计（前端消费满时低频标记，不静默丢弃）
_dropped_frames = 0
_last_drop_marker = 0.0


async def broadcast(msg: dict, exclude: asyncio.Queue | None = None) -> None:
    """广播给所有前端连接（日志消息只投递给订阅者）。"""
    global _dropped_frames, _last_drop_marker
    is_log = msg.get("type") in ("log", "log_end", "log_reset")
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


def _clear_log_history(container: str) -> None:
    _log_history.pop(container, None)
    _log_history_bytes.pop(container, None)


def _cache_log(msg: dict) -> None:
    """缓存有界日志段，供同一 Agent 流上的后续前端订阅者回放。"""
    container = str(msg.get("container") or "")
    if not container or container not in _log_subscribers:
        return
    size = len(str(msg.get("line") or "").encode("utf-8", errors="replace")) + 64
    history = _log_history.setdefault(container, deque())
    history.append((dict(msg), size))
    total = _log_history_bytes.get(container, 0) + size
    while history and (
        len(history) > LOG_HISTORY_MAX_MESSAGES or total > LOG_HISTORY_MAX_BYTES
    ):
        _, removed = history.popleft()
        total -= removed
    _log_history_bytes[container] = total


def _replay_log_history(container: str, q: asyncio.Queue) -> None:
    """无 await 地把当前缓存排入新订阅者队列，保证与随后实时消息的顺序。"""
    for msg, _ in _log_history.get(container, ()):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            break


def _reset_and_replay_log_history(container: str, q: asyncio.Queue) -> None:
    """重置单个前端的日志视图并回放缓存，修复页面重入时的重复订阅。"""
    try:
        q.put_nowait({"type": "log_reset", "container": container})
    except asyncio.QueueFull:
        return
    _replay_log_history(container, q)


def _finish_log_stream(node_id: int, container: str, generation: int) -> None:
    """Agent 日志进程结束后释放订阅状态，使页面可再次订阅已重启容器。"""
    key = (node_id, container)
    current = _log_generations.get(key)
    if current is None or generation != current:
        return
    subscribers = _log_subscribers.pop(container, set())
    for q in subscribers:
        _frontend_queues.get(q, set()).discard(container)
    _agent_log_subs.discard(key)
    _clear_log_history(container)


def unregister_frontend(q: asyncio.Queue) -> None:
    """前端断开：清理其日志订阅；容器无订阅者时向 agent 退订日志流。"""
    _frontend_queues.pop(q, None)
    containers = {c for c, subs in _log_subscribers.items() if q in subs}
    for c in containers:
        _log_subscribers[c].discard(q)
        if not _log_subscribers[c]:
            _log_subscribers.pop(c, None)
            _clear_log_history(c)
            # 容器名全局唯一（每任务每节点一个容器），可反查所属节点
            db = SessionLocal()
            try:
                tn = (db.query(TaskNode)
                      .filter(TaskNode.container_name == c).first())
                node_id = tn.node_id if tn else None
            finally:
                db.close()
            if node_id is not None:
                key = (node_id, c)
                _agent_log_subs.discard(key)
                generation = _log_generations.get(key)
                if generation is not None:
                    asyncio.create_task(_agent_send_cmd(
                        c, "log_unsubscribe", node_id=node_id,
                        generation=generation,
                    ))


async def subscribe_log(node_id: int, container: str, q: asyncio.Queue,
                        tail: int = LOG_REPLAY_TAIL) -> None:
    """前端订阅容器日志：注册转发目标；agent 未开流时下发订阅命令。

    首个订阅由 agent 的同一条流回放历史并无缝追踪新行；后续订阅者直接回放
    控制平面缓存。复用同一前端连接的重复订阅也会重置视图并回放缓存，以容忍
    页面切换时丢失退订消息。这样既没有 HTTP 快照/WS 订阅空窗，也不会产生双源重复行。
    """
    if not container:
        return
    if len(_frontend_queues.get(q, set())) >= MAX_LOG_SUBS_PER_CLIENT:
        logger.warning("前端连接订阅容器数达上限 %d，拒绝订阅 %s", MAX_LOG_SUBS_PER_CLIENT, container)
        return
    client_subs = _frontend_queues.setdefault(q, set())
    subscribers = _log_subscribers.setdefault(container, set())
    if container in client_subs and q in subscribers:
        # SPA 页面切换时退订消息可能未及时送达，同一浏览器 WS 会继续保留旧
        # 订阅。重新进入详情页后显式订阅应刷新视图并回放离开期间的缓存，
        # 不能直接当作无操作，否则静默容器会一直显示“暂无日志”。
        _reset_and_replay_log_history(container, q)
        return
    client_subs.add(container)
    subscribers.add(q)
    key = (node_id, container)
    first = key not in _agent_log_subs
    _agent_log_subs.add(key)  # 登记需求：断连重连后据此补发
    try:
        tail = max(0, min(int(tail), MAX_LOG_REPLAY_TAIL))
    except (TypeError, ValueError):
        tail = LOG_REPLAY_TAIL
    if not first:
        _replay_log_history(container, q)
        return
    generation = _log_generations.get(key, 0) + 1
    _log_generations[key] = generation
    _clear_log_history(container)
    if first and is_connected(node_id):
        await _agent_send_cmd(
            container, "log_subscribe", node_id=node_id, tail=tail,
            generation=generation,
        )


async def unsubscribe_log(node_id: int, container: str, q: asyncio.Queue) -> None:
    subs = _log_subscribers.get(container)
    if subs:
        subs.discard(q)
        if not subs:
            _log_subscribers.pop(container, None)
            _clear_log_history(container)
            _agent_log_subs.discard((node_id, container))
            generation = _log_generations.get((node_id, container))
            if generation is not None:
                await _agent_send_cmd(
                    container, "log_unsubscribe", node_id=node_id,
                    generation=generation,
                )
    _frontend_queues.get(q, set()).discard(container)


# ---------- Agent 连接管理 ----------


async def _agent_send_cmd(container: str, cmd: str, generation: int,
                          node_id: int | None = None,
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
    msg["generation"] = generation
    await _send(ws, msg)


async def _handle_message(node: Node, msg: dict) -> None:
    # 任一类消息都刷新心跳时间戳（metrics 每 5s 必来，最弱的持续心跳源）
    _last_msg_ts[node.id] = time.time()
    mtype = msg.get("type")
    if mtype == "metrics":
        await _on_metrics(node, msg.get("data") or {})
    elif mtype == "docker_event":
        await _on_docker_event(node, msg.get("data") or {})
    elif mtype == "log":
        container = str(msg.get("container") or "")
        try:
            generation = int(msg["generation"])
        except (KeyError, TypeError, ValueError):
            return
        current = _log_generations.get((node.id, container))
        if current is None or generation != current:
            return
        _cache_log(msg)
        await broadcast(msg)
    elif mtype == "log_end":
        container = str(msg.get("container") or "")
        try:
            generation = int(msg["generation"])
        except (KeyError, TypeError, ValueError):
            return
        current = _log_generations.get((node.id, container))
        if current is None or generation != current:
            return
        await broadcast(msg)
        _finish_log_stream(node.id, container, generation)
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
    """Agent 拉取进度：控制端→head 或 head→worker，均持久化并广播。"""
    kind = msg.get("kind")
    key = msg.get("key", "")
    written = msg.get("written") or 0
    db = SessionLocal()
    try:
        if kind == "model":
            transfer_id, separator, relpath = str(key).partition(":")
            if not (separator and transfer_id.isdigit() and relpath):
                return
            job = db.get(ModelDownload, int(transfer_id))
            if job and job.head_node_id == node.id:
                _model_file_progress[(job.id, node.id, relpath)] = written
                written = sum(
                    value for (jid, nid, _), value in _model_file_progress.items()
                    if jid == job.id and nid == node.id
                )
                job.sent_bytes = min(written, job.total_bytes or written)
                db.commit()
                await broadcast({"type": "transfer_progress", "kind": "model",
                                 "job_id": job.id, "sent_bytes": job.sent_bytes,
                                 "total_bytes": job.total_bytes})
        elif kind == "image":
            t = (db.query(ImageTransfer)
                 .filter(ImageTransfer.digest == key,
                         ImageTransfer.status.in_(
                             ["pulling", "packing", "sending", "syncing", "loading", "paused"]))
                 .order_by(ImageTransfer.id.desc()).first())
            if t and t.head_node_id == node.id:
                t.sent_bytes = written
                db.commit()
                await broadcast({"type": "transfer_progress", "kind": "image",
                                 "job_id": t.id, "sent_bytes": written,
                                 "total_bytes": t.size_bytes})
        elif kind == "image-sync" and str(key).isdigit():
            t = db.get(ImageTransfer, int(key))
            if t and t.status in ("syncing", "paused"):
                jobs = dict(t.sync_jobs or {})
                info = dict(jobs.get(str(node.id)) or {})
                info.update(
                    status="syncing",
                    transferred_bytes=written,
                    total_bytes=msg.get("total") or t.size_bytes or 0,
                )
                jobs[str(node.id)] = info
                t.sync_jobs = jobs
                db.commit()
                await broadcast({
                    "type": "transfer_progress",
                    "kind": "image-sync",
                    "job_id": t.id,
                    "node_id": node.id,
                    "sent_bytes": written,
                    "total_bytes": info["total_bytes"],
                })
        elif kind == "model-sync" and str(key).isdigit():
            job = db.get(ModelDownload, int(key))
            if job and job.status in ("syncing", "paused"):
                jobs = dict(job.sync_jobs or {})
                info = dict(jobs.get(str(node.id)) or {})
                info.update(
                    status="syncing",
                    transferred_bytes=written,
                    total_bytes=msg.get("total") or job.total_bytes or 0,
                )
                jobs[str(node.id)] = info
                job.sync_jobs = jobs
                db.commit()
                await broadcast({
                    "type": "transfer_progress", "kind": "model-sync",
                    "job_id": job.id, "node_id": node.id,
                    "sent_bytes": written, "total_bytes": info["total_bytes"],
                })
    finally:
        db.close()


def _ws_additional_headers(node: Node) -> list[tuple[str, str]]:
    """控制平面 -> Agent WS 握手携带该节点自己的 token（agent 侧校验）。"""
    return [("Authorization", f"Bearer {node.agent_token or ''}")]


def _ws_connect_options(node: Node) -> dict:
    """局域网 Agent 连接参数；显式禁用系统代理以保证私网直连。"""
    return {
        "proxy": None,
        "ping_interval": 20,
        "ping_timeout": 20,
        "open_timeout": 10,
        "max_size": 4 * 1024 * 1024,
        "additional_headers": _ws_additional_headers(node),
    }


async def _connect_node(node: Node) -> None:
    """单节点连接循环：连接 -> 收消息分发 -> 断连退避重连。"""
    backoff = 1
    task = asyncio.current_task()
    while not _stop.is_set():
        # 每次重连前从 DB 刷新节点：部署即轮换会更新 agent_token，持旧引用的
        # 重连循环会一直用旧 token 握手（agent 侧 4401）直到 backend 重启。
        with SessionLocal() as db:
            fresh = db.get(Node, node.id)
            if fresh is None:
                logger.warning("WS 连接 agent %s：节点已删除，退出连接循环", node.name)
                return
            node = fresh
        url = f"ws://{node.ip}:{node.agent_port}/ws/events"
        try:
            # Agent 位于管理局域网，不能继承控制平面的 HTTP(S)_PROXY；否则
            # 私网 WebSocket 会被错误送往代理，表现为 503 / 永久离线。
            async with websockets.connect(url, **_ws_connect_options(node)) as ws:
                task._ws = ws  # 供订阅控制命令复用当前连接
                _connected[node.id] = True
                _last_msg_ts[node.id] = time.time()
                backoff = 1
                await _set_node_status(node.id, "online")
                logger.info("WS 已连接 agent %s (%s)", node.name, node.ip)
                # agent 侧日志流随断连终止：重连后补发所有仍被订阅的容器
                for nid, container in list(_agent_log_subs):
                    if nid == node.id:
                        key = (nid, container)
                        generation = _log_generations.get(key, 0) + 1
                        _log_generations[key] = generation
                        _clear_log_history(container)
                        await broadcast({"type": "log_reset", "container": container})
                        await _send(ws, {"type": "log_subscribe",
                                         "container": container,
                                         "tail": LOG_REPLAY_TAIL,
                                         "generation": generation})
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception as e:
                        logger.debug("忽略 agent %s 的无效 WS 消息: %s", node.name, e)
                        continue
                    try:
                        await _handle_message(node, msg)
                    except Exception:
                        logger.exception("agent %s WS 消息处理失败", node.name)
                task._ws = None
                _connected[node.id] = False
                await _set_node_status(node.id, "offline")
                logger.warning("WS 断开 agent %s，%.0fs 后重连", node.name, backoff)
        except asyncio.CancelledError:
            task._ws = None
            _connected[node.id] = False
            raise
        except Exception as e:
            task._ws = None
            _connected[node.id] = False
            await _set_node_status(node.id, "offline")
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
        except Exception:
            logger.exception("WS 连接同步失败")
        await asyncio.sleep(SYNC_INTERVAL)


async def _watchdog_pass() -> None:
    """单轮看门狗扫描（独立成函数便于单测）。

    对 WS 连接存活但超过 NODE_STALE_TIMEOUT 无任何消息的节点：
    判定离线（清除连接态 + 落库广播 node_status）+ 主动关闭 WS 触发重连循环，
    覆盖「连接活着但不推数据」的悬挂场景（如 agent 采集子进程卡死）。
    """
    now = time.time()
    for node_id, last_ts in list(_last_msg_ts.items()):
        if not _connected.get(node_id, False):
            continue
        if now - last_ts <= config.NODE_STALE_TIMEOUT:
            continue
        logger.warning("节点 %d WS 心跳超时（>%ss 无消息），判定离线并强制重连",
                       node_id, config.NODE_STALE_TIMEOUT)
        _connected[node_id] = False
        await _set_node_status(node_id, "offline")
        conn = _conn_tasks.get(node_id)
        ws = getattr(conn, "_ws", None) if conn else None
        if ws is not None:
            try:
                await ws.close()
            except Exception as e:
                logger.debug("关闭节点 %d 的超时 WS 连接失败: %s", node_id, e)


async def _watchdog_loop() -> None:
    while not _stop.is_set():
        try:
            await _watchdog_pass()
        except Exception:
            logger.exception("WS 心跳看门狗扫描失败")
        await asyncio.sleep(WATCHDOG_INTERVAL)


async def start() -> None:
    _stop.clear()
    background_tasks.spawn(_sync_connections())
    background_tasks.spawn(_watchdog_loop())


async def stop() -> None:
    _stop.set()
    for task in _conn_tasks.values():
        task.cancel()
    _conn_tasks.clear()
    _connected.clear()
    _last_msg_ts.clear()
    _log_generations.clear()
