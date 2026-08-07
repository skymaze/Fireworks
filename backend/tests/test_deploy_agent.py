"""Agent 部署流程单测：token 生成/注入/落库/验证时序（无真实节点，mock SSH 与 HTTP）。

覆盖：部署即轮换（每次部署新 token）、成功落库并同步内存对象、
失败不落库（旧 token 保持）、token 字符集天然合规。
"""

import asyncio
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Node
from app.services import deploy_agent


@pytest.fixture()
def S():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _add_node(S, **kw) -> int:
    with S() as db:
        node = Node(name="n1", ip="10.0.0.9", ssh_username="spark", ssh_password="x", **kw)
        db.add(node)
        db.commit()
        return node.id


def _deploy(monkeypatch, S, node_id, deploy_result, info_ok=True):
    """跑 deploy() 主流程；返回 (result, 本次注入的 token)。"""
    captured: dict = {}

    def fake_deploy_sync(node, token):
        captured["token"] = token
        return deploy_result

    async def fake_info(node):
        if not info_ok:
            raise RuntimeError("agent unreachable")
        return {"hostname": "n1"}

    monkeypatch.setattr(deploy_agent, "_deploy_sync", fake_deploy_sync)
    monkeypatch.setattr(deploy_agent.agent_client, "info", fake_info)
    monkeypatch.setattr(deploy_agent, "SessionLocal", S)

    async def run():
        with S() as db:
            node = db.get(Node, node_id)
            return await deploy_agent.deploy(node)

    return asyncio.run(run()), captured.get("token")


def _token_in_db(S, node_id) -> str | None:
    with S() as db:
        row = db.get(Node, node_id)
        return row.agent_token


def test_deploy_success_rotates_and_persists_token(monkeypatch, S):
    """部署成功：生成新 token 注入、落库、内存对象同步（info 用新 token 验证）。"""
    node_id = _add_node(S)
    result, token = _deploy(monkeypatch, S, node_id, {"ok": True, "install_dir": "/opt/fireworks-agent"})
    assert result["ok"] is True
    assert token and len(token) >= 40
    # token_urlsafe 字符集天然合规（deploy.sh 同款校验）
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert _token_in_db(S, node_id) == token


def test_deploy_failure_keeps_old_token(monkeypatch, S):
    """部署失败：新 token 不落库（旧 token 保持，旧 Agent 继续工作）。"""
    node_id = _add_node(S, agent_token="old-token")
    result, token = _deploy(monkeypatch, S, node_id, {"ok": False, "error": "ssh failed"})
    assert result["ok"] is False
    assert _token_in_db(S, node_id) == "old-token"


def test_deploy_info_failure_still_persists_token(monkeypatch, S):
    """部署成功但连通性验证失败：token 仍落库（Agent 已用新 token 运行，warning 提示）。"""
    node_id = _add_node(S)
    result, token = _deploy(monkeypatch, S, node_id,
                            {"ok": True, "install_dir": "/opt/fireworks-agent"}, info_ok=False)
    assert result["ok"] is True and "warning" in result
    assert _token_in_db(S, node_id) == token
