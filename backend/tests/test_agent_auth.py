"""Agent 鉴权回归：Agent 侧 token 中间件/WS 校验 + 后端客户端 token 注入。

- Agent 侧：未带/错误 token 一律 401；正确 token（Bearer / X-Agent-Token / ?token=）放行；
  /api/health 探针放行；未配置 DGX_AGENT_TOKEN 时 fail closed；
  /ws/events 未认证以 4401 关闭。
- 后端侧：agent_client 所有请求带 Authorization Bearer；agent_ws 握手带 extra_headers。
"""

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDisconnect

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import main as agent_main  # noqa: E402

from app.models import Node  # noqa: E402
from app.services import agent_client, agent_ws  # noqa: E402

AUTH = {"Authorization": "Bearer agent-test-token"}


@pytest.fixture(autouse=True)
def _agent_token(monkeypatch):
    monkeypatch.setattr(agent_main, "AGENT_TOKEN", "agent-test-token")


# ---------- Agent 侧 HTTP ----------


def test_health_open_without_token():
    c = TestClient(agent_main.app)
    assert c.get("/api/health").status_code == 200


def test_http_requires_token():
    c = TestClient(agent_main.app)
    # 未带 token / 错误 token 一律拒绝
    assert c.get("/api/containers").status_code == 401
    assert c.post("/api/containers/x/action", json={"action": "pause"},
                  headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/containers", headers={"X-Agent-Token": "wrong"}).status_code == 401
    assert c.get("/api/containers?token=wrong").status_code == 401


def test_http_allows_correct_token():
    c = TestClient(agent_main.app)
    # 探针：container_action 在跑 docker 前先校验 action，返回 400 说明已通过鉴权到达路由
    for headers in (AUTH, {"X-Agent-Token": "agent-test-token"}):
        r = c.post("/api/containers/x/action", json={"action": "not-real"},
                   headers=headers)
        assert r.status_code == 400, r.text
    r = c.post("/api/containers/x/action?token=agent-test-token",
               json={"action": "not-real"})
    assert r.status_code == 400


def test_fail_closed_without_configured_token(monkeypatch):
    """未下发 DGX_AGENT_TOKEN 时即使携带 token 也拒绝（fail closed）。"""
    monkeypatch.setattr(agent_main, "AGENT_TOKEN", "")
    c = TestClient(agent_main.app)
    assert c.get("/api/containers", headers=AUTH).status_code == 401


# ---------- Agent 侧 WS ----------


def test_ws_rejects_unauthenticated():
    c = TestClient(agent_main.app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with c.websocket_connect("/ws/events") as ws:
            ws.receive_json()
    assert ei.value.code == 4401


def test_ws_allows_correct_token():
    c = TestClient(agent_main.app)
    with c.websocket_connect("/ws/events", headers=AUTH) as ws:
        # 无关容器退订是 no-op；若能发送不抛异常即代表握手已通过鉴权
        ws.send_json({"type": "log_unsubscribe", "container": "nope"})


# ---------- 后端客户端 token 注入 ----------


def test_agent_client_sends_node_bearer(monkeypatch):
    """控制平面 -> Agent 请求携带该节点自己的 token。"""
    captured: dict = {}

    async def fake_request(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kw.get("headers")
        return _FakeResp()

    monkeypatch.setattr(agent_client._client, "request", fake_request)
    node = Node(id=1, name="n1", ip="192.0.2.1", agent_port=9000, agent_token="tok-123")
    asyncio.run(agent_client._request("GET", node, "/api/info"))
    assert captured["headers"]["Authorization"] == "Bearer tok-123"


def test_agent_client_sends_empty_bearer_without_token(monkeypatch):
    """节点未部署（无 token）时发空 Bearer——Agent 侧 fail closed 拒绝，防御正确。"""
    captured: dict = {}

    async def fake_request(method, url, **kw):
        captured["headers"] = kw.get("headers")
        return _FakeResp()

    monkeypatch.setattr(agent_client._client, "request", fake_request)
    node = Node(id=1, name="n1", ip="192.0.2.1", agent_port=9000)
    asyncio.run(agent_client._request("GET", node, "/api/info"))
    assert captured["headers"]["Authorization"] == "Bearer "


def test_agent_ws_extra_headers(monkeypatch):
    node = Node(id=1, name="n1", ip="192.0.2.1", agent_port=9000, agent_token="tok-123")
    assert agent_ws._ws_additional_headers(node) == [("Authorization", "Bearer tok-123")]


@pytest.mark.anyio
async def test_ws_connect_carries_token_on_handshake(monkeypatch):
    """真实 websockets.connect(additional_headers=...) 在握手时携带节点 token。

    守护 websockets 版本把 extra_headers 改名等参数漂移：若参数不存在会在此测试暴露。
    """
    import websockets

    node = Node(id=1, name="n1", ip="192.0.2.1", agent_port=9000, agent_token="tok-123")
    seen: dict = {}

    async def process_request(connection, request):
        seen["authorization"] = request.headers.get("Authorization")
        return None

    async def handler(websocket):
        await websocket.send(await websocket.recv())
        await websocket.close()

    async with websockets.serve(handler, "127.0.0.1", 0,
                                process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/ws/events",
            additional_headers=agent_ws._ws_additional_headers(node),
        ) as ws:
            await ws.send("ping")
            echo = await ws.recv()
        assert echo == "ping"
    assert seen.get("authorization") == "Bearer tok-123"


class _FakeResp:
    def raise_for_status(self):
        return None

    def json(self):
        return {}
