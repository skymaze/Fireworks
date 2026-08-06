"""前端 WebSocket 端点：实时状态广播 + 任务日志流订阅。

协议：
- 服务端 -> 前端：metrics / container_status / task_status / transfer_progress / log / log_end
- 前端 -> 服务端：log_subscribe {task_id, node_id} / log_unsubscribe {task_id, node_id}
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import config
from ..db import SessionLocal
from ..models import TaskNode
from ..security import track_ws, untrack_ws, ws_cookie_user
from ..services import agent_ws

logger = logging.getLogger(__name__)

router = APIRouter()

# WebSocket 未认证关闭码（前端据此识别登录态失效）
WS_UNAUTHORIZED = 4401
# Origin 校验失败关闭码（跨站连接拒绝）
WS_ORIGIN_REJECTED = 4403


def _origin_allowed(ws: WebSocket) -> bool:
    """WS 握手 Origin 校验（跨站 WS 劫持的纵深防御，配合 SameSite=Lax cookie）。

    空 Origin（同源 Nitro 代理等非浏览器客户端）放行；带 Origin 的浏览器连接
    必须与请求的 Host 同源，或在 CORS_ORIGINS 白名单内，否则拒绝。
    """
    origin = (ws.headers.get("origin") or "").strip()
    if not origin:
        return True
    from urllib.parse import urlsplit

    o = urlsplit(origin)
    if not o.hostname:
        return False
    if origin in config.CORS_ORIGINS:
        return True
    host = ws.headers.get("host") or ""
    h = urlsplit(f"//{host}" if not host.startswith("http") else host)
    return bool(h.hostname) and h.hostname == o.hostname


def _container_of(task_id: int, node_id: int) -> str | None:
    db = SessionLocal()
    try:
        tn = (db.query(TaskNode)
              .filter_by(task_id=task_id, node_id=node_id).first())
        return tn.container_name if tn else None
    finally:
        db.close()


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    # 先 accept 再校验会话 cookie：无效/过期立即以 4401 关闭（若在 accept 前 close，
    # Starlette 会对握手返回 HTTP 403，客户端收不到 WS 关闭码 4401）
    await ws.accept()
    if not _origin_allowed(ws):
        await ws.close(code=WS_ORIGIN_REJECTED)
        return
    token = ws.cookies.get(config.SESSION_COOKIE)
    db = SessionLocal()
    try:
        user = ws_cookie_user(ws.cookies, db)
    finally:
        db.close()
    if user is None:
        await ws.close(code=WS_UNAUTHORIZED)
        return
    # 登记实时连接：登出/会话吊销时由 security 主动关闭（防止已建立连接继续收广播）
    if token:
        track_ws(token, ws)
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    agent_ws.register_frontend(q)

    async def sender():
        try:
            while True:
                msg = await q.get()
                await ws.send_json(msg)
        except Exception:  # noqa: BLE001 - 连接关闭
            pass

    send_task = asyncio.create_task(sender())
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype in ("log_subscribe", "log_unsubscribe"):
                task_id = msg.get("task_id")
                node_id = msg.get("node_id")
                if not task_id or not node_id:
                    continue
                container = _container_of(int(task_id), int(node_id))
                if not container:
                    continue
                if mtype == "log_subscribe":
                    await agent_ws.subscribe_log(int(node_id), container, q)
                else:
                    await agent_ws.unsubscribe_log(int(node_id), container, q)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.debug("前端 WS 异常关闭", exc_info=True)
    finally:
        send_task.cancel()
        agent_ws.unregister_frontend(q)
        if token:
            untrack_ws(token, ws)
