"""项目审查问题的定向回归测试。"""

import asyncio
import base64
import re

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import schemas
from app.db import Base
from app.models import Cluster, Node, Recipe, TaskIdentity
from app.routers import clusters, tasks
from app.services import network_config
from app.services import network_test


def test_network_apply_script_imports_shutil(monkeypatch):
    """已有 Netplan 配置触发备份分支时，远端脚本必须能调用 shutil.copy。"""
    captured = {}

    def fake_sudo(_node, inner, timeout=120):
        captured["inner"] = inner
        return "WROTE_DROPIN\nAPPLY_OK\n", ""

    monkeypatch.setattr(network_config, "_sudo_exec", fake_sudo)
    node = Node(id=1, name="n1", ip="192.0.2.1")
    plan = network_config.plan_cluster_network("10.100.0.0/16")
    ok, _ = network_config.apply_node_network(node, plan, 1)
    assert ok
    encoded = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d > /tmp/fw_net_apply_[a-f0-9]+\.py", captured["inner"])
    assert encoded
    script = base64.b64decode(encoded.group(1)).decode()
    assert "import os, glob, json, base64, yaml, subprocess, shutil" in script
    assert "value.get('match')" in script
    assert "run_netplan('generate')" in script
    assert "FW_APPLY_ERROR" in script


def test_cluster_create_duplicate_nodes_rolls_back_cluster():
    """成员占用失败时不得留下已经提交的空集群。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add(Node(id=1, name="n1", ip="192.0.2.1"))
        db.commit()
        req = schemas.ClusterCreate(name="duplicate", node_ids=[1, 1])
        with pytest.raises(HTTPException) as exc:
            clusters.create_cluster(req, db)
        assert exc.value.status_code == 400
        assert db.query(Cluster).filter_by(name="duplicate").first() is None
        db.refresh(db.get(Node, 1))
        assert db.get(Node, 1).cluster_id is None
    finally:
        db.close()


def test_cluster_network_reuses_existing_config(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        nodes = [
            Node(id=1, name="n1", ip="192.0.2.1"),
            Node(id=2, name="n2", ip="192.0.2.2"),
        ]
        db.add_all(nodes)
        db.commit()
        plan = network_config.plan_cluster_network("10.10.0.0/16", 9000)
        monkeypatch.setattr(
            network_config,
            "analyze_existing_cluster_network",
            lambda *_args: {
                "mode": "reuse",
                "plan": plan,
                "node_indices": {1: 1, 2: 2},
                "networks": [{"cidr": plan["cidr"], "mtu": 9000, "node_ids": [1, 2]}],
            },
        )
        monkeypatch.setattr(network_config, "inspect_node_network", lambda *_args: {})
        monkeypatch.setattr(
            network_config, "probe_cluster_physical_links",
            lambda *_args: {"ok": True, "issues": []},
        )
        monkeypatch.setattr(network_config, "probe_plan_ip_conflicts", lambda *_args: [])
        monkeypatch.setattr(network_config, "verify_node_network", lambda *_args: (True, {}))
        monkeypatch.setattr(
            network_config,
            "apply_node_network",
            lambda *_args: pytest.fail("现有网络不应被重写"),
        )

        actual_plan, applied, indices = clusters._configure_cluster_network(
            db, [2, 1], "10.0.0.0/16", 9000
        )
        assert actual_plan == plan
        assert applied == []
        assert indices == {1: 1, 2: 2}
    finally:
        db.close()


def test_cluster_network_rejects_database_cidr_conflict():
    """最终选定的网段与已有集群重叠时必须阻断，不能静默放行。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add(Cluster(name="existing", network_cidr="10.10.0.0/16"))
        db.commit()
        with pytest.raises(HTTPException) as exc:
            clusters._ensure_cidr_available(db, "10.10.20.0/24")
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "cidr_conflict"
        assert exc.value.detail["params"] == {
            "cidr": "10.10.20.0/24",
            "name": "existing",
            "free": "10.11.0.0/24",
        }
    finally:
        db.close()


def test_cluster_network_unexpected_error_rolls_back_applied_nodes(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add_all([
            Node(id=1, name="n1", ip="192.0.2.1"),
            Node(id=2, name="n2", ip="192.0.2.2"),
        ])
        db.commit()
        rolled_back = []
        monkeypatch.setattr(
            network_config,
            "analyze_existing_cluster_network",
            lambda *_args: {"mode": "configure", "networks": []},
        )
        monkeypatch.setattr(network_config, "inspect_node_network", lambda *_args: {})
        monkeypatch.setattr(
            network_config, "probe_cluster_physical_links",
            lambda *_args: {"ok": True, "issues": []},
        )
        monkeypatch.setattr(network_config, "probe_plan_ip_conflicts", lambda *_args: [])

        def apply(node, _plan, _index):
            if node.id == 2:
                raise RuntimeError("netplan crashed")
            return True, "ok"

        monkeypatch.setattr(network_config, "apply_node_network", apply)
        monkeypatch.setattr(
            network_config,
            "rollback_node_network",
            lambda node: (rolled_back.append(node.id) or True, "ok"),
        )
        with pytest.raises(HTTPException) as exc:
            clusters._configure_cluster_network(db, [1, 2], "10.20.0.0/16", 9000)
        assert exc.value.detail["code"] == "network_configure_failed"
        assert rolled_back == [1]
    finally:
        db.close()


def test_mixed_existing_networks_require_fresh_cidr(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add_all([
            Node(id=1, name="n1", ip="192.0.2.1"),
            Node(id=2, name="n2", ip="192.0.2.2"),
        ])
        db.commit()
        monkeypatch.setattr(
            network_config,
            "analyze_existing_cluster_network",
            lambda *_args: {
                "mode": "reconfigure",
                "networks": [
                    {"cidr": "10.0.0.0/16", "mtu": 9000, "node_ids": [1]},
                    {"cidr": "10.10.0.0/16", "mtu": 9000, "node_ids": [2]},
                ],
            },
        )
        monkeypatch.setattr(network_config, "inspect_node_network", lambda *_args: {})
        monkeypatch.setattr(
            network_config, "probe_cluster_physical_links",
            lambda *_args: {"ok": True, "issues": []},
        )
        with pytest.raises(HTTPException) as exc:
            clusters._configure_cluster_network(db, [1, 2], "10.0.0.0/16", 9000)
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "network_reconfig_cidr_conflict"
        assert exc.value.detail["params"]["suggested"] == "10.1.0.0/16"
    finally:
        db.close()


def test_task_assignment_schema_rejects_invalid_role_and_rank():
    with pytest.raises(ValidationError):
        schemas.TaskNodeAssignment(node_id=1, role="other", node_rank=1)
    with pytest.raises(ValidationError):
        schemas.TaskNodeAssignment(node_id=1, role="worker", node_rank=-1)


def test_ping_network_test_uses_planned_peer_roce_ip(monkeypatch):
    seen = {}

    async def fake_network_test(_node, payload, duration=10):
        seen.update(payload)
        return {"rc": 0, "output": "ok"}

    monkeypatch.setattr(network_test.agent_client, "network_test", fake_network_test)
    source = Node(id=1, name="n1", ip="192.0.2.1")
    target = Node(id=2, name="n2", ip="192.0.2.2")
    result = asyncio.run(network_test.run_network_test(
        source, target, "ping", duration=2,
        roce_ip_override="10.1.0.10",
        peer_roce_ip_override="10.1.0.13",
    ))
    assert result["from"] == "n1" and result["to"] == "n2"
    assert seen["server_host"] == "10.1.0.13"


def test_task_create_rejects_duplicate_nodes_before_deploy():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add(Cluster(id=1, name="c1", network_type="roce"))
        db.add(Recipe(id=1, name="r1", compose_template="services: {}", variables=[]))
        db.commit()
        req = schemas.TaskCreate(
            name="t1", recipe_id=1, cluster_id=1,
            nodes=[
                {"node_id": 1, "role": "head", "node_rank": 0},
                {"node_id": 1, "role": "worker", "node_rank": 1},
            ],
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(tasks.create_task(req, db))
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "task_node_duplicated"
    finally:
        db.close()


def test_sqlite_index_migration_is_idempotent(monkeypatch):
    """旧库缺少约束/查询索引时，迁移可重复执行且使用模型声明的名称。"""
    from app import main

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX uq_cluster_nodes_node"))
        conn.execute(sa.text("DROP INDEX ix_inference_ts"))
        conn.execute(sa.text(
            "INSERT INTO task_benchmarks (task_id, ts, result) VALUES (999, 1, '{}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO inference_samples (task_id, node_id, ts, data) "
            "VALUES (999, 999, 1, '{}')"
        ))
    monkeypatch.setattr(main, "engine", engine)
    main._migrate_sqlite()
    main._migrate_sqlite()
    indexes = {i["name"] for i in sa.inspect(engine).get_indexes("cluster_nodes")}
    assert "uq_cluster_nodes_node" in indexes
    inference_indexes = {
        i["name"] for i in sa.inspect(engine).get_indexes("inference_samples")
    }
    assert "ix_inference_ts" in inference_indexes
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT MAX(id) FROM task_identities"
        )).scalar_one() == 999
        assert conn.execute(sa.text(
            "SELECT COUNT(*) FROM task_benchmarks WHERE task_id = 999"
        )).scalar_one() == 0
        assert conn.execute(sa.text(
            "SELECT COUNT(*) FROM inference_samples WHERE task_id = 999"
        )).scalar_one() == 0
    S = sessionmaker(bind=engine)
    with S() as db:
        identity = TaskIdentity()
        db.add(identity)
        db.flush()
        assert identity.id == 1000
