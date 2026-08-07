"""认证与安全基础件：密码散列、登录会话、节点 Agent token、登录限速、审计日志。

- 密码使用 bcrypt 散列；
- 会话为不透明 token（secrets.token_urlsafe），DB 仅存 sha256 摘要，
  登录时写入 HttpOnly cookie；token 原文泄露面最小化（DB 泄露无法伪造会话）；
- 节点 Agent 使用**每节点独立 token**（部署时生成注入，部署即轮换），
  控制平面→Agent 请求与 Agent 回拉均以 Bearer header 携带；
- 登录失败按来源 IP 限速，防爆破；
- audit() 记录关键操作，绝不落密码/token/SSH 凭据等敏感值。
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from datetime import timedelta

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .errors import Code, api_error
from .models import AuthSession, Node, User, utcnow

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("fireworks.audit")

SESSION_TTL = timedelta(hours=config.SESSION_TTL_HOURS)

# 依赖返回值中代表「Agent 身份」的哨兵（供区分来源，端点本身不使用）
AGENT = object()


def audit(action: str, **fields):
    """审计日志：关键操作留痕，敏感值一律不进日志。"""
    detail = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    audit_logger.info("%s %s", action, detail)


# ---------- 密码 ----------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# ---------- 节点 Agent token ----------


def node_for_token(candidate: str | None, db: Session) -> Node | None:
    """按 token 恒时比较识别节点（Agent 回拉鉴权用）。

    低频操作（仅模型/镜像回拉时调用），O(n) 遍历可接受；命中即证明
    该请求来自系统内已部署的节点，且携带的是它自己的 token（节点间不可冒充）。
    """
    if not candidate:
        return None
    for node in db.query(Node).all():
        if node.agent_token and hmac.compare_digest(
            candidate.encode(), node.agent_token.encode()
        ):
            return node
    return None


# ---------- 会话 ----------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int, db: Session) -> str:
    """为新会话签发 token，返回原文（调用方写入 cookie）。顺带懒清理过期会话。"""
    cutoff = utcnow() - SESSION_TTL
    db.query(AuthSession).filter(AuthSession.expires_at < cutoff).delete(
        synchronize_session=False
    )
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            user_id=user_id,
            token_hash=_token_hash(token),
            created_at=utcnow(),
            expires_at=utcnow() + SESSION_TTL,
        )
    )
    db.commit()
    return token


def _user_for_token(token: str | None, db: Session) -> User | None:
    if not token:
        return None
    row = (
        db.query(AuthSession)
        .filter(AuthSession.token_hash == _token_hash(token))
        .first()
    )
    if not row:
        return None
    if utcnow() > row.expires_at:
        db.delete(row)
        db.commit()
        return None
    return row.user


def revoke_session(token: str | None, db: Session):
    if not token:
        return
    db.query(AuthSession).filter(AuthSession.token_hash == _token_hash(token)).delete()
    db.commit()
    # 吊销即关闭：已建立的实时连接（WS）不能继续接收广播（见 _live_ws）
    _close_ws_for_token(token)


# ---------- 已建立实时连接的会话吊销联动 ----------
#
# WS 只在握手时校验会话；若登出/证书吊销只删 session 行，已建立的长连接仍会持续
# 收到广播。维护 token_hash -> 活动 WS（及其事件循环）的注册表，吊销会话时主动关闭。

_live_ws: dict[str, set[tuple[asyncio.AbstractEventLoop, object]]] = {}


def track_ws(token: str, ws: object):
    """记录一条已认证的实时连接（ws_events 校验通过后调用）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    _live_ws.setdefault(_token_hash(token), set()).add((loop, ws))


def untrack_ws(token: str, ws: object):
    """连接结束时移除（幂等）。"""
    bucket = _live_ws.get(_token_hash(token))
    if not bucket:
        return
    for entry in list(bucket):
        if entry[1] is ws:
            bucket.discard(entry)
    if not bucket:
        _live_ws.pop(_token_hash(token), None)


async def _close_ws(ws: object):
    try:
        await ws.close(code=4401)  # type: ignore[attr-defined]
    except Exception:
        pass


def _close_ws_for_token(token: str):
    """关闭该会话名下所有仍活着的实时连接。

    revoke_session 可能运行在请求事件循环或线程池（sync 端点）中，通过
    loop.call_soon_threadsafe 调度到连接所属的事件循环执行关闭。
    """
    for loop, ws in _live_ws.pop(_token_hash(token), set()):
        if loop is None:
            continue
        try:
            loop.call_soon_threadsafe(
                lambda target=ws, l=loop: l.create_task(_close_ws(target))
            )
        except Exception:  # noqa: BLE001 - 连接已关闭等，忽略
            pass


# ---------- FastAPI 认证依赖 ----------


def _token_from_request(request: Request) -> str | None:
    """会话 token 来源：优先 cookie，其次 Authorization: Bearer（便于脚本/API 调用）。"""
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """保护业务端点：必须持有有效登录会话，否则 401。"""
    user = _user_for_token(_token_from_request(request), db)
    if user is None:
        raise api_error(401, Code.UNAUTHORIZED, "未登录或会话已过期")
    return user


def get_user_or_agent(request: Request, db: Session = Depends(get_db)):
    """Agent 回拉端点专用：有效登录会话 或 系统内节点的 Agent token 均可通过。

    token 只从 Authorization: Bearer 头读取（不认 ?token= query——token 进 URL
    会落入反代/网关访问日志）。节点身份由 node_for_token 识别。
    """
    user = _user_for_token(_token_from_request(request), db)
    if user is not None:
        return user
    bearer = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if node_for_token(bearer, db) is not None:
        return AGENT
    raise api_error(401, Code.AGENT_TOKEN_INVALID, "未登录或 Agent token 无效")


def ws_cookie_user(cookies: dict, db: Session) -> User | None:
    """WebSocket 握手校验：从握手请求 cookie 解析会话（WS 无法走 HTTP 依赖注入）。"""
    return _user_for_token(cookies.get(config.SESSION_COOKIE), db)


# ---------- 登录限速（内存 per-IP） ----------


class LoginRateLimiter:
    """按来源 IP 计失败的次数窗口；命中上限后直接拒绝（429）。

    内存实现、单进程有效——控制平面默认单 uvicorn worker，够用。
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 900):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._fails: dict[str, list[float]] = {}

    def is_blocked(self, key: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._fails.get(key, []) if now - t < self.window_seconds]
        self._fails[key] = recent
        return len(recent) >= self.max_attempts

    def record_failure(self, key: str):
        self._fails.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str):
        self._fails.pop(key, None)


login_limiter = LoginRateLimiter()
