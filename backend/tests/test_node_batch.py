"""批量节点操作（batch/deploy-agent、batch/optimize）：并行执行、逐节点结果互不影响。

无真实节点，直接调用 router 函数 + 假 DB + monkeypatch 部署/优化，覆盖：
- 批量部署：全部成功 → 逐节点结果 + 状态回写 online + hardware_info 落库；
- 批量部署局部失败：失败节点不阻断/不污染其余节点，仅自身结果 ok=False；
- 批量部署全部缺失 → 404 node_not_found；部分缺失 → 缺失项进结果不阻断；
- 批量优化：并行执行、成功落 optimize_result；
- 批量优化失败重跑：不覆盖既有「已优化」记录，未优化节点落失败结果便于提示。
"""

import asyncio

import pytest
from fastapi import HTTPException

from app import schemas
from app.errors import Code
from app.models import Node
from app.routers import nodes


class _FakeDB:
    """内存假 DB：仅实现 batch 路由用到的 get(Node, id) 与 commit。"""

    def __init__(self, *node_list: Node):
        self.by_id = {n.id: n for n in node_list}
        self.commits = 0

    def get(self, model, node_id):
        if model is Node:
            return self.by_id.get(node_id)
        return None

    def commit(self):
        self.commits += 1


def _node(nid: int, name: str) -> Node:
    return Node(id=nid, name=name, ip=f"10.0.0.{nid}", ssh_username="spark",
                ssh_auth_type="password", ssh_password="x", agent_port=9000)


# ---------- 批量部署 Agent ----------


def test_batch_deploy_all_success_parallel_and_persists(monkeypatch):
    """批量部署全部成功：逐节点结果、deploy 逐个执行、状态与硬件信息回写。"""
    deployed: list[Node] = []

    async def fake_deploy(node):
        deployed.append(node)
        return {"ok": True, "hardware_info": {"hostname": node.name, "agent_version": "1.0.0"}}

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    n1, n2 = _node(1, "a"), _node(2, "b")
    db = _FakeDB(n1, n2)
    res = asyncio.run(nodes.batch_deploy_agents(schemas.BatchNodesRequest(node_ids=[1, 2]), db))
    assert len(deployed) == 2
    assert [r["node_id"] for r in res["results"]] == [1, 2]
    assert all(r["ok"] for r in res["results"])
    assert res["ok_count"] == 2 and res["failed_count"] == 0
    assert n1.agent_status == "online" and n2.agent_status == "online"
    assert n1.hardware_info["agent_version"] == "1.0.0"
    assert db.commits >= 1


def test_batch_deploy_partial_failure_isolates(monkeypatch):
    """批量部署局部失败：失败节点仅自身 ok=False，成功节点照常回写。"""
    async def fake_deploy(node):
        if node.id == 2:
            return {"ok": False, "error": "ssh connect failed"}
        return {"ok": True, "hardware_info": {"hostname": node.name}}

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    n1, n2 = _node(1, "a"), _node(2, "b")
    db = _FakeDB(n1, n2)
    res = asyncio.run(nodes.batch_deploy_agents(schemas.BatchNodesRequest(node_ids=[1, 2]), db))
    assert res["results"][0]["ok"] is True
    assert res["results"][1]["ok"] is False
    assert res["results"][1]["error"] == "ssh connect failed"
    assert res["ok_count"] == 1 and res["failed_count"] == 1
    assert n1.agent_status == "online"
    assert n1.hardware_info is not None
    assert n2.agent_status is None  # 失败节点状态不被改写


def test_batch_deploy_all_missing_404():
    """全部节点缺失：整体 404 node_not_found。"""
    db = _FakeDB()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.batch_deploy_agents(schemas.BatchNodesRequest(node_ids=[9, 10]), db))
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == Code.NODE_NOT_FOUND


def test_batch_deploy_partial_missing_keeps_rest(monkeypatch):
    """部分节点缺失：不阻断，缺失项进结果（ok=False），其余节点正常部署。"""
    async def fake_deploy(node):
        return {"ok": True, "hardware_info": {"hostname": node.name}}

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    n1 = _node(1, "a")
    db = _FakeDB(n1)
    res = asyncio.run(nodes.batch_deploy_agents(schemas.BatchNodesRequest(node_ids=[1, 99]), db))
    assert len(res["results"]) == 2
    by_id = {r["node_id"]: r for r in res["results"]}
    assert by_id[1]["ok"] is True
    assert by_id[99]["ok"] is False and "不存在" in by_id[99]["error"]
    assert res["ok_count"] == 1 and res["failed_count"] == 1


def test_batch_deploy_deduplicates_ids(monkeypatch):
    """重复 id 去重：同一个节点只部署一次。"""
    calls: list[int] = []

    async def fake_deploy(node):
        calls.append(node.id)
        return {"ok": True, "hardware_info": {"hostname": node.name}}

    monkeypatch.setattr(nodes.deploy_agent, "deploy", fake_deploy)
    n1 = _node(1, "a")
    db = _FakeDB(n1)
    res = asyncio.run(nodes.batch_deploy_agents(schemas.BatchNodesRequest(node_ids=[1, 1, 1]), db))
    assert calls == [1]
    assert len(res["results"]) == 1
    assert res["ok_count"] == 1


# ---------- 批量初始优化 ----------


def _fake_optimize_result(node: Node, *, ok: bool, summary: str) -> dict:
    return {"ok": ok, "ran_at": "2026-08-12T00:00:00+00:00", "steps": [],
            "summary": summary, "warnings": ["unreachable"] if not ok else []}


def test_batch_optimize_all_success_persists(monkeypatch):
    """批量优化全部成功：并行执行每个节点、成功结果逐一落 optimize_result。"""
    optimized: list[Node] = []

    def fake_optimize(node):
        optimized.append(node)
        return _fake_optimize_result(node, ok=True, summary=f"done {node.name}")

    monkeypatch.setattr(nodes.node_optimize, "optimize_node", fake_optimize)
    n1, n2, n3 = _node(1, "a"), _node(2, "b"), _node(3, "c")
    db = _FakeDB(n1, n2, n3)
    res = asyncio.run(nodes.batch_optimize_nodes(schemas.BatchNodesRequest(node_ids=[1, 2, 3]), db))
    assert len(optimized) == 3
    assert all(r["ok"] for r in res["results"])
    assert res["ok_count"] == 3 and res["failed_count"] == 0
    assert n1.optimize_result["summary"] == "done a"
    assert n2.optimize_result["summary"] == "done b"


def test_batch_optimize_failure_keeps_previous_state(monkeypatch):
    """失败重跑不覆盖既有「已优化」记录；未优化节点落失败结果便于界面临时提示。"""
    def fake_optimize(node):
        return _fake_optimize_result(node, ok=(node.id == 2), summary=("ok2" if node.id == 2 else "fail1"))

    monkeypatch.setattr(nodes.node_optimize, "optimize_node", fake_optimize)
    n1 = _node(1, "a")
    n1.optimize_result = {"ok": True, "ran_at": "earlier", "summary": "already ok",
                          "steps": [], "warnings": []}
    n2 = _node(2, "b")
    db = _FakeDB(n1, n2)
    res = asyncio.run(nodes.batch_optimize_nodes(schemas.BatchNodesRequest(node_ids=[1, 2]), db))
    assert res["results"][0]["ok"] is False
    assert res["results"][1]["ok"] is True
    assert n1.optimize_result["summary"] == "already ok"  # 不被失败重跑覆盖
    assert n2.optimize_result["summary"] == "ok2"
    assert res["ok_count"] == 1 and res["failed_count"] == 1
