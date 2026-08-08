"""认证 API：初始化建号 / 登录 / 登出 / 修改密码 / 状态查询。

阶段一为单一用户：无注册、无角色、无多用户管理。
- 数据表无任何用户时允许 POST /setup 创建初始账号（首次部署引导建号）；
- 登录成功签发 HttpOnly 会话 cookie；
- 登录/建号/改密均受来源 IP 限速保护。
"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import config, security
from ..db import get_db
from ..errors import Code, api_error
from ..models import AuthSession, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key=config.SESSION_COOKIE,
        value=token,
        max_age=int(security.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        path="/",
        secure=config.COOKIE_SECURE,
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    """登录页/守卫查询：初始化是否需要、当前是否已登录。

    始终返回 200（不触发前端的全局 401 跳转循环）。
    """
    setup_required = db.query(User).count() == 0
    user = security._user_for_token(security._token_from_request(request), db)
    return {
        "setup_required": setup_required,
        "authenticated": user is not None,
        "username": user.username if user else None,
    }


@router.post("/setup", status_code=201)
def setup_account(req: SetupRequest, response: Response, request: Request,
                  db: Session = Depends(get_db)):
    """首次部署初始化：仅当库中尚无任何用户时可调用，创建唯一账号并直接登录。"""
    if security.login_limiter.is_blocked(_client_ip(request)):
        raise api_error(429, Code.RATE_LIMITED, "尝试过于频繁，请稍后再试")
    usernames = [u.username for u in db.query(User.username).all()]
    if usernames:
        raise api_error(403, Code.ALREADY_INITIALIZED, "账号已初始化，不能重复创建")
    username = req.username.strip()
    if not username:
        raise api_error(422, Code.USERNAME_EMPTY, "用户名不能为空")
    if username in usernames:
        raise api_error(409, Code.USERNAME_EXISTS, "用户名已存在")
    try:
        user = User(username=username, password_hash=security.hash_password(req.password))
        db.add(user)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, Code.INIT_CONFLICT, "账号初始化冲突，请刷新后重试")
    db.refresh(user)
    _set_session_cookie(response, security.create_session(user.id, db))
    security.audit("auth.setup", username=username, ip=_client_ip(request))
    return {"ok": True, "username": username}


@router.post("/login")
def login(req: LoginRequest, response: Response, request: Request,
          db: Session = Depends(get_db)):
    key = _client_ip(request)
    if security.login_limiter.is_blocked(key):
        raise api_error(429, Code.RATE_LIMITED, "尝试过于频繁，请稍后再试")
    user = db.query(User).filter(User.username == req.username).first()
    if user is None or not security.verify_password(req.password, user.password_hash):
        security.login_limiter.record_failure(key)
        security.audit("auth.login_failed", username=req.username, ip=key)
        raise api_error(401, Code.BAD_CREDENTIALS, "用户名或密码错误")
    security.login_limiter.reset(key)
    _set_session_cookie(response, security.create_session(user.id, db))
    security.audit("auth.login", username=user.username, ip=key)
    return {"ok": True, "username": user.username}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = security._token_from_request(request)
    user = security._user_for_token(token, db)
    security.revoke_session(token, db)
    response.delete_cookie(
        config.SESSION_COOKIE, path="/", secure=config.COOKIE_SECURE
    )
    if user:
        security.audit("auth.logout", username=user.username, ip=_client_ip(request))
    return {"ok": True}


@router.post("/change-password")
def change_password(req: ChangePasswordRequest, request: Request, response: Response,
                    db: Session = Depends(get_db)):
    """修改密码（需已登录）。改密后吊销该用户全部会话（除当前浏览器重新签发），
    防止泄露会话继续可用。"""
    key = _client_ip(request)
    if security.login_limiter.is_blocked(key):
        raise api_error(429, Code.RATE_LIMITED, "尝试过于频繁，请稍后再试")
    user = security.get_current_user(request, db)
    if not security.verify_password(req.old_password, user.password_hash):
        security.login_limiter.record_failure(key)
        security.audit("auth.change_password_failed", username=user.username, ip=key)
        raise api_error(403, Code.OLD_PASSWORD_WRONG, "原密码错误")
    user.password_hash = security.hash_password(req.new_password)
    # 吊销该用户全部会话，再为当前浏览器重新签发（updated_at 由 onupdate 自动维护）
    db.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
    db.commit()
    _set_session_cookie(response, security.create_session(user.id, db))
    security.login_limiter.reset(key)
    security.audit("auth.change_password", username=user.username, ip=key)
    return {"ok": True}
