"""添加节点即安装 Agent（create_node 原子语义）：部署成功才落库、失败明确报错并回滚。

无真实节点，直接调用 router 函数 + 假 DB + monkeypatch 部署/卸载，覆盖：
- 部署成功（含 hardware_info）→ 节点置 online，不删除；
- 部署失败 → 422 agent_install_failed，节点行被回滚删除；
- 安装完成但连通性验证失败 → 先卸载远端 Agent，再 400 agent_verify_failed_rollback，
  节点行被回滚删除；
- 同名节点 → 409，部署不被执行。
"""

import asyncio

import pytest
from fastapi import HTTPException

from app import schemas
from app.errors import Code
from app.models import Node
from app.routers import nodes


class _AllNoneQ:
    """query().filter().first() 恒返回 None（无同名节点）。"""

    def filter(self, *a, **kw):
        return self

    def first(self):
        return None


class _FakeDB:
    def __init__(self, first_result=_AllNoneQ()):
        self.deleted: list[Node] = []
        self._q = first_result

    def query(self, model):
        return self._q

    def add(self, obj):  # 不真正入库
        pass

    def commit(self):
        pass

    def refresh(self, obj):
        pass

    def delete(self, obj):
        self.deleted.append(obj)


def _req(**kw) -> schemas.NodeCreate:
    base = dict(name="n1", ip="10.0.0.9", ssh_username="spark",
                ssh_auth_type="password", ssh_password="x")
    base.update(kw)
    return schemas.NodeCreate(**base)


@pytest.fixture(autouse=True)
def _noop_optimize(monkeypatch):
    """默认把初始优化置为 no-op（真实优化走 SSH，测试无节点）。

    仅显式覆盖的用例才真正校验优化逻辑；优化是同步函数，
    由 _run_optimize_best_effort 经 asyncio.to_thread 调用。
    """

    def fake_optimize(node):
        return {"ok": True, "ran_at": "2026-08-12T00:00:00+00:00",
                "steps": [], "summary": "noop", "warnings": []}

    monkeypatch.setattr(nodes.node_optimize, "optimize_node", fake_optimize)


def test_create_success_deploys_agent_and_marks_online(monkeypatch):
    """部署成功：落库 hardware_info、置 online，节点保留。"""
    hw = {"hostname": "n1", "agent_version": "0.1.0"}

    async def fake_deploy(node):
        node.agent_token = "tok-123"
        return {"ok": True, "hardware_info": hw}

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    db = _FakeDB()
    created = asyncio.run(nodes.create_node(_req(), db))
    assert created.agent_status == "online"
    assert created.hardware_info == hw
    assert db.deleted == []


def test_create_install_failure_rolls_back(monkeypatch):
    """部署失败（如 SSH 不可达）：422 agent_install_failed，节点行被回滚删除。"""

    async def fake_deploy(node):
        return {"ok": False, "error": "ssh connect failed"}

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    db = _FakeDB()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.create_node(_req(), db))
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == Code.AGENT_INSTALL_FAILED
    assert "ssh connect failed" in ei.value.detail["details"]
    assert len(db.deleted) == 1  # 节点已回滚


def test_create_verify_failure_uninstalls_then_rolls_back(monkeypatch):
    """安装完成但连通性验证失败：先卸载远端 Agent，再 400 报错并回滚节点。"""
    uninstalled: list[Node] = []
    warn = "部署完成但 Agent 连通性验证失败: timeout"

    async def fake_deploy(node):
        node.agent_token = "tok-123"
        return {"ok": True, "warning": warn}

    async def fake_uninstall(node):
        uninstalled.append(node)

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    monkeypatch.setattr(nodes.deploy_agent, "uninstall", fake_uninstall)
    db = _FakeDB()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.create_node(_req(), db))
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == Code.AGENT_VERIFY_FAILED_ROLLBACK
    assert warn in ei.value.detail["details"]
    assert len(uninstalled) == 1  # 远端残留已尽力清理
    assert len(db.deleted) == 1   # 节点已回滚


def test_create_verify_uninstall_error_still_rolls_back(monkeypatch):
    """卸载 Agent 也失败时：不阻断报错回滚（尽力而为语义）。"""
    uninstalled = []

    async def fake_deploy(node):
        node.agent_token = "tok-123"
        return {"ok": True, "warning": "unreachable"}

    async def fake_uninstall(node):
        uninstalled.append(node)
        raise RuntimeError("ssh 卸载失败")

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    monkeypatch.setattr(nodes.deploy_agent, "uninstall", fake_uninstall)
    db = _FakeDB()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.create_node(_req(), db))
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == Code.AGENT_VERIFY_FAILED_ROLLBACK
    assert len(uninstalled) == 1
    assert len(db.deleted) == 1


def test_create_duplicate_name_rejected_and_not_deployed(monkeypatch):
    """同名节点已存在：409 node_name_exists，部署不被执行。"""

    class _ExistsQ:
        def filter(self, *a, **kw):
            return self

        def first(self):
            return object()  # 已存在同名

    calls: list[Node] = []

    async def fake_deploy(node):
        calls.append(node)
        return {"ok": True, "hardware_info": {"hostname": "n1"}}

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    db = _FakeDB(first_result=_ExistsQ())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.create_node(_req(name="dup"), db))
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == Code.NODE_NAME_EXISTS
    assert calls == []


# ---------- 添加节点时的初始优化（best-effort，不阻断添加） ----------


def test_create_optimize_runs_and_persists(monkeypatch):
    """optimize_on_add 默认开启：部署成功后执行优化并落 optimize_result。"""
    hw = {"hostname": "n1", "agent_version": "0.1.0"}
    optimize_result = {"ok": True, "ran_at": "t", "steps": [], "summary": "s", "warnings": []}
    calls: list[Node] = []

    async def fake_deploy(node):
        node.agent_token = "tok-123"
        return {"ok": True, "hardware_info": hw}

    def fake_optimize(node):
        calls.append(node)
        return optimize_result

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    monkeypatch.setattr(nodes.node_optimize, "optimize_node", fake_optimize)
    db = _FakeDB()
    created = asyncio.run(nodes.create_node(_req(), db))
    assert created.optimize_result == optimize_result
    assert len(calls) == 1
    assert db.deleted == []


def test_create_optimize_off_skipped(monkeypatch):
    """optimize_on_add=False：不执行优化，optimize_result 保持 None。"""
    called: list[Node] = []

    async def fake_deploy(node):
        node.agent_token = "tok-123"
        return {"ok": True, "hardware_info": {"hostname": "n1"}}

    def fake_optimize(node):
        called.append(node)
        return {"ok": True, "steps": []}

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    monkeypatch.setattr(nodes.node_optimize, "optimize_node", fake_optimize)
    db = _FakeDB()
    created = asyncio.run(nodes.create_node(_req(optimize_on_add=False), db))
    assert created.optimize_result is None
    assert called == []


def test_create_optimize_failure_keeps_node(monkeypatch):
    """优化抛错：best-effort 收敛为结果 dict，不阻断添加、不回滚节点。"""
    hw = {"hostname": "n1", "agent_version": "0.1.0"}

    async def fake_deploy(node):
        node.agent_token = "tok-123"
        return {"ok": True, "hardware_info": hw}

    def fake_optimize(node):
        raise RuntimeError("ssh boom")

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    monkeypatch.setattr(nodes.node_optimize, "optimize_node", fake_optimize)
    db = _FakeDB()
    created = asyncio.run(nodes.create_node(_req(), db))
    assert created.optimize_result["ok"] is False
    assert "ssh boom" in created.optimize_result["warnings"][0]
    assert db.deleted == []  # 节点保留
    assert created.agent_status == "online"
