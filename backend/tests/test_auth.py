"""认证与安全加固集成测试（独立内存库 + 依赖覆盖，不触碰真实数据库文件）。

覆盖：首启建号 / 登录 / 登出 / 改密 / 业务路由强制认证 / CORS 收紧 /
登录限速防爆破 / Agent token 回拉端点门控 / WebSocket 会话校验（4401）。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import WebSocketDisconnect

from app import config, security
from app.db import Base, get_db
from app.main import app
from app.routers import ws as ws_router
from app.services import agent_ws

PASSWORD = "SuperSecret123"
AGENT_TOKEN = "test-agent-token"


@pytest.fixture(autouse=True)
def _clean_limiter():
    """登录限速器与活动 WS 注册表是模块级单例，测试间重置，避免相互影响。"""
    security.login_limiter._fails.clear()
    security._live_ws.clear()
    yield
    security.login_limiter._fails.clear()
    security._live_ws.clear()


@pytest.fixture()
def env(monkeypatch):
    """独立内存库（StaticPool 保证 TestClient 多线程共享同一连接）+ 依赖覆盖。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)

    def _test_db():
        db = S()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _test_db
    # WS 路由直接用 SessionLocal()（不走 get_db），单独替换
    monkeypatch.setattr(ws_router, "SessionLocal", S)
    # 固定 Agent token，避免 get_agent_token 落到真实库
    monkeypatch.setattr(config, "AGENT_TOKEN_ENV", AGENT_TOKEN)
    client = TestClient(app)
    yield client, S
    app.dependency_overrides.clear()


def _setup(client: TestClient) -> None:
    r = client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": PASSWORD},
    )
    assert r.status_code == 201
    assert "fw_session=" in r.headers.get("set-cookie", "")


# ---------- 基础：状态与强制认证 ----------


def test_initial_status(env):
    client, _ = env
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json() == {"setup_required": True, "authenticated": False, "username": None}


def test_business_routes_require_auth(env):
    client, _ = env
    for path in ["/api/overview", "/api/nodes", "/api/clusters", "/api/recipes",
                 "/api/tasks", "/api/models/settings", "/api/images/settings",
                 "/api/nodes/1/containers", "/api/models/sync/xyz"]:
        assert client.get(path).status_code == 401, path
    assert client.post("/api/nodes", json={}).status_code == 401
    assert client.delete("/api/recipes/1").status_code == 401


def test_health_is_public(env):
    client, _ = env
    assert client.get("/api/health").status_code == 200


def test_setup_login_logout_flow(env):
    client, _ = env
    _setup(client)
    # 建号后 cookie 生效，可访问业务端点
    assert client.get("/api/overview").status_code == 200
    # 已初始化：重复 setup 拒绝
    assert client.post(
        "/api/auth/setup", json={"username": "admin2", "password": PASSWORD}
    ).status_code == 403
    # 登出后失效
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/overview").status_code == 401
    # 正确密码可重新登录
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    ).status_code == 200
    assert client.get("/api/overview").status_code == 200


def test_login_wrong_password(env):
    client, _ = env
    _setup(client)
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"})
    assert r.status_code == 401


def test_change_password(env):
    client, S = env
    _setup(client)
    # 原密码错误 -> 403
    assert client.post(
        "/api/auth/change-password",
        json={"old_password": "nope", "new_password": "NewPassword1"},
    ).status_code == 403
    # 改密成功，旧密码失效、新密码可登录
    assert client.post(
        "/api/auth/change-password",
        json={"old_password": PASSWORD, "new_password": "NewPassword1"},
    ).status_code == 200
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    ).status_code == 401
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": "NewPassword1"}
    ).status_code == 200


def test_login_rate_limit(env):
    client, _ = env
    _setup(client)
    client.post("/api/auth/logout")
    for _ in range(5):
        assert client.post(
            "/api/auth/login", json={"username": "admin", "password": "bad-pass"}
        ).status_code == 401
    # 第 6 次被限速
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    ).status_code == 429
    # setup 同样受限速保护
    assert client.post(
        "/api/auth/setup", json={"username": "x", "password": PASSWORD}
    ).status_code == 429


# ---------- CORS 收紧 ----------


def test_cors_restricted(env):
    client, _ = env
    preflight_headers = {
        "Origin": "http://evil.example",
        "Access-Control-Request-Method": "GET",
    }
    r = client.options("/api/overview", headers=preflight_headers)
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_frontend(env):
    client, _ = env
    if "http://localhost:3000" in config.CORS_ORIGINS:
        r = client.options(
            "/api/overview",
            headers={"Origin": "http://localhost:3000",
                     "Access-Control-Request-Method": "GET"},
        )
        assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ---------- Agent 回拉端点（user_or_agent 门控） ----------


def test_internal_rejects_invalid_agent_token(env):
    client, _ = env
    assert client.get("/api/images/archive/1?token=wrong").status_code == 401
    assert client.get("/api/models/files/o/m/x?relpath=a&token=wrong").status_code == 401
    assert client.get("/api/images/archive/1").status_code == 401


def test_internal_allows_valid_agent_token(env):
    client, _ = env
    # 过了认证门控后才进入业务逻辑：归档不存在 -> 404（而非 401）
    assert client.get(f"/api/images/archive/999?token={AGENT_TOKEN}").status_code == 404
    # models/files 非法 relpath -> 400（而非 401）
    assert client.get(
        f"/api/models/files/foo?relpath=../x&token={AGENT_TOKEN}"
    ).status_code == 400


def test_internal_allows_user_session(env):
    client, _ = env
    _setup(client)
    assert client.get("/api/images/archive/999").status_code == 404
    assert client.get("/api/models/files/foo?relpath=../x").status_code == 400


# ---------- WebSocket 会话校验 ----------


def test_ws_rejects_unauthenticated(env):
    client, _ = env
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws/events") as ws:
            ws.receive_json()
    assert ei.value.code == 4401


def test_ws_allows_authenticated(env, monkeypatch):
    client, _ = env
    _setup(client)

    def fake_register(q):
        q.put_nowait({"type": "test", "payload": "hello"})

    monkeypatch.setattr(agent_ws, "register_frontend", fake_register)
    with client.websocket_connect("/ws/events") as ws:
        msg = ws.receive_json()
    assert msg == {"type": "test", "payload": "hello"}


def test_ws_closed_after_logout(env):
    """登出后已建立的实时连接被服务端主动关闭（4401），不再继续收广播。"""
    client, _ = env
    _setup(client)
    with client.websocket_connect("/ws/events") as ws:
        # 登出吊销会话 -> security 主动关闭该会话名下所有存活 WS
        client.post("/api/auth/logout")
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 4401


# ---------- 结构化错误码（RFC 9457 风格：code + msg） ----------


def _detail(r):
    """兼容字符串或对象两种 detail。"""
    d = r.json().get("detail")
    return d if isinstance(d, dict) else {"code": None, "msg": d}


def test_error_codes_auth(env):
    """认证类错误带稳定 code，前端据此本地化。"""
    client, _ = env
    _setup(client)
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"})
    assert _detail(r)["code"] == "bad_credentials"
    client.post("/api/auth/logout")
    r = client.get("/api/overview")
    assert _detail(r)["code"] == "unauthorized"
    # 未初始化场景
    client2, _ = env
    r = client2.get("/api/overview")
    assert _detail(r)["code"] == "unauthorized"


def test_error_codes_node_and_cluster(env):
    """资源不存在类错误带稳定 code。"""
    client, _ = env
    _setup(client)
    r = client.get("/api/nodes/99999")
    assert _detail(r)["code"] == "node_not_found"
    r = client.post("/api/nodes", json={"name": "n1", "ip": "10.0.0.9",
                                        "ssh_username": "spark", "ssh_password": "x"})
    assert r.status_code == 201
    r = client.post("/api/nodes", json={"name": "n1", "ip": "10.0.0.10",
                                        "ssh_username": "spark", "ssh_password": "x"})
    assert _detail(r)["code"] == "node_name_exists"

