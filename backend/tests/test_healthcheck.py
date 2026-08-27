"""健康检查（compose healthcheck 优先）回归测试。

覆盖：
- aggregate_task_health 判定（healthy/unhealthy/starting/no-check/采集失败保守）
- collect_container_health 采集映射
- task_monitor._check_task：unhealthy -> error、healthy 恢复 running、exited -> stopped
- tasks._health_check：healthy -> running、unhealthy -> error、no-check 降级/直接就绪
- Agent compose_ps 返回容器 Health（docker inspect）
全部内存库 / mock，不触碰真实 docker 与网络。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Node, Task, TaskNode
from app.services import task_monitor


def _mem_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _task(db, status="running"):
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status=status,
                variables={}, rendered={"nodes": {"10": {"project": "t1", "role": "head",
                                                         "env": {"VLLM_PORT": "8888"}}}}))
    db.add(Node(id=10, name="n1", ip="192.0.2.10", agent_port=9000))
    db.add(TaskNode(task_id=1, node_id=10, role="head", node_rank=0,
                    container_name="t1-rank0"))
    db.commit()


# ---------- aggregate_task_health ----------


def test_aggregate_health_unhealthy_wins():
    sigs = [{"node_name": "a", "health": "healthy"},
            {"node_name": "b", "health": "unhealthy"}]
    assert task_monitor.aggregate_task_health(sigs) == "unhealthy"


def test_aggregate_health_all_checked_healthy():
    sigs = [{"node_name": "a", "health": "healthy"},
            {"node_name": "b", "health": "healthy"}]
    assert task_monitor.aggregate_task_health(sigs) == "healthy"


def test_aggregate_health_starting_waits():
    sigs = [{"node_name": "a", "health": "healthy"},
            {"node_name": "b", "health": "starting"}]
    assert task_monitor.aggregate_task_health(sigs) == "starting"


def test_aggregate_health_mixed_checked_and_unchecked():
    """部分容器声明健康检查、部分未声明：只按声明的判定。"""
    sigs = [{"node_name": "a", "health": "healthy"},
            {"node_name": "b", "health": ""}]
    assert task_monitor.aggregate_task_health(sigs) == "healthy"


def test_aggregate_health_no_check():
    sigs = [{"node_name": "a", "health": ""},
            {"node_name": "b", "health": ""}]
    assert task_monitor.aggregate_task_health(sigs) == "no-check"


def test_aggregate_health_ignores_unavailable_signal():
    """采集失败（None）与未配置（""）同等不参与判定：有健康声明的按声明判定。"""
    sigs = [{"node_name": "a", "health": "healthy"},
            {"node_name": "b", "health": None}]
    assert task_monitor.aggregate_task_health(sigs) == "healthy"
    sigs2 = [{"node_name": "a", "health": ""}, {"node_name": "b", "health": None}]
    assert task_monitor.aggregate_task_health(sigs2) == "no-check"


# ---------- collect_container_health ----------


@pytest.mark.anyio
async def test_collect_container_health_maps_health(monkeypatch):
    db = _mem_db()
    _task(db)
    task = db.get(Task, 1)

    async def fake_ps(node, project):
        return {"containers": [
            {"name": "t1-rank0", "state": "running", "health": "healthy"},
        ]}

    monkeypatch.setattr("app.services.agent_client.compose_ps", fake_ps)
    out = await task_monitor.collect_container_health(db, task)
    assert out == [{"node_name": "n1", "container": "t1-rank0", "health": "healthy"}]
    db.close()


# ---------- task_monitor._check_task ----------


@pytest.mark.anyio
async def test_monitor_unhealthy_sets_error(monkeypatch):
    db = _mem_db()
    _task(db, status="running")

    async def fake_ps(node, project):
        return {"containers": [
            {"name": "t1-rank0", "state": "running", "health": "unhealthy"},
        ]}

    monkeypatch.setattr("app.services.agent_client.compose_ps", fake_ps)
    monkeypatch.setattr("app.services.task_monitor.SessionLocal", lambda: db)
    await task_monitor._check_task(1)
    t = db.get(Task, 1)
    assert t.status == "error"
    assert "健康检查失败" in (t.error or "")
    assert t.health == "unhealthy"
    db.close()


@pytest.mark.anyio
async def test_monitor_healthy_recovers_error(monkeypatch):
    db = _mem_db()
    _task(db, status="error")
    db.query(Task).filter_by(id=1).update({"error": "健康检查超时：…未就绪"})
    db.commit()

    async def fake_ps(node, project):
        return {"containers": [
            {"name": "t1-rank0", "state": "running", "health": "healthy"},
        ]}

    monkeypatch.setattr("app.services.agent_client.compose_ps", fake_ps)
    monkeypatch.setattr("app.services.task_monitor.SessionLocal", lambda: db)
    await task_monitor._check_task(1)
    assert db.get(Task, 1).status == "running"
    assert db.get(Task, 1).error is None
    assert db.get(Task, 1).health == "healthy"
    db.close()


@pytest.mark.anyio
async def test_monitor_all_exited_sets_stopped(monkeypatch):
    db = _mem_db()
    _task(db, status="running")

    async def fake_ps(node, project):
        return {"containers": [
            {"name": "t1-rank0", "state": "exited", "health": ""},
        ]}

    monkeypatch.setattr("app.services.agent_client.compose_ps", fake_ps)
    monkeypatch.setattr("app.services.task_monitor.SessionLocal", lambda: db)
    await task_monitor._check_task(1)
    assert db.get(Task, 1).status == "stopped"
    db.close()


# ---------- tasks._health_check ----------


@pytest.mark.anyio
async def test_health_check_healthy_sets_running(monkeypatch):
    from app.routers import tasks as tasks_router

    db = _mem_db()
    _task(db, status="published")
    monkeypatch.setattr("app.routers.tasks.SessionLocal", lambda: db)

    async def fake_collect(s_db, task):
        return [{"node_name": "n1", "container": "t1-rank0", "health": "healthy"}]

    monkeypatch.setattr(task_monitor, "collect_container_health", fake_collect)
    await tasks_router._health_check(1, 10)
    assert db.get(Task, 1).status == "running"
    db.close()


@pytest.mark.anyio
async def test_health_check_unhealthy_sets_error(monkeypatch):
    from app.routers import tasks as tasks_router

    db = _mem_db()
    _task(db, status="published")
    monkeypatch.setattr("app.routers.tasks.SessionLocal", lambda: db)

    async def fake_collect(s_db, task):
        return [{"node_name": "n1", "container": "t1-rank0", "health": "unhealthy"}]

    monkeypatch.setattr(task_monitor, "collect_container_health", fake_collect)
    await tasks_router._health_check(1, 10)
    t = db.get(Task, 1)
    assert t.status == "error"
    assert "健康检查失败" in (t.error or "")
    db.close()


@pytest.mark.anyio
async def test_health_check_no_check_keeps_running_without_probe(monkeypatch):
    """配方未声明 healthcheck：保持原状态、显示「未配置」，不再做任何端口/路径探测。"""
    from app.routers import tasks as tasks_router

    db = _mem_db()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running",
                variables={}, rendered={"nodes": {"10": {"project": "t1", "role": "head",
                                                         "env": {"VLLM_PORT": "8888"}}}}))
    db.add(Node(id=10, name="n1", ip="192.0.2.10", agent_port=9000))
    db.add(TaskNode(task_id=1, node_id=10, role="head", node_rank=0,
                    container_name="t1-rank0"))
    db.commit()
    monkeypatch.setattr("app.routers.tasks.SessionLocal", lambda: db)

    async def fake_collect(s_db, task):
        return [{"node_name": "n1", "container": "t1-rank0", "health": ""}]

    async def boom(*a, **k):
        raise AssertionError("不该再做 vLLM /v1/models 探测")

    monkeypatch.setattr(task_monitor, "collect_container_health", fake_collect)
    monkeypatch.setattr("app.services.agent_client.http_get", boom)
    await tasks_router._health_check(1, 10)
    assert db.get(Task, 1).status == "running"  # 保持原状态
    assert db.get(Task, 1).health == ""  # 未配置
    db.close()


@pytest.mark.anyio
async def test_collect_container_health_head_only(monkeypatch):
    """健康只采集 head 节点容器；worker 不参与（节点只有状态）。"""
    db = _mem_db()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running",
                variables={}, rendered={"nodes": {
                    "10": {"project": "t1", "role": "head", "env": {"VLLM_PORT": "8888"}},
                    "11": {"project": "t1", "role": "worker", "env": {}},
                }}))
    db.add(Node(id=10, name="n-head", ip="192.0.2.10", agent_port=9000))
    db.add(Node(id=11, name="n-worker", ip="192.0.2.11", agent_port=9000))
    db.add(TaskNode(task_id=1, node_id=10, role="head", node_rank=0, container_name="t1-head"))
    db.add(TaskNode(task_id=1, node_id=11, role="worker", node_rank=1, container_name="t1-worker"))
    db.commit()

    async def fake_ps(node, project):
        return {"containers": [
            {"name": "t1-head", "state": "running", "health": "healthy"},
            {"name": "t1-worker", "state": "running", "health": "unhealthy"},
        ]}

    monkeypatch.setattr("app.services.agent_client.compose_ps", fake_ps)
    out = await task_monitor.collect_container_health(db, db.get(Task, 1))
    # 只采集 head，即便 worker 返回 unhealthy 也不进入健康信号
    assert len(out) == 1 and out[0]["container"] == "t1-head"
    assert out[0]["health"] == "healthy"
    db.close()


@pytest.mark.anyio
async def test_monitor_worker_unhealthy_does_not_error_task(monkeypatch):
    """worker 容器不参与健康判定：head healthy 时任务保持 running，不因 worker 置 error。"""
    db = _mem_db()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running",
                variables={}, rendered={"nodes": {
                    "10": {"project": "t1", "role": "head", "env": {}},
                    "11": {"project": "t1", "role": "worker", "env": {}},
                }}))
    db.add(Node(id=10, name="n-head", ip="192.0.2.10", agent_port=9000))
    db.add(Node(id=11, name="n-worker", ip="192.0.2.11", agent_port=9000))
    db.add(TaskNode(task_id=1, node_id=10, role="head", node_rank=0, container_name="t1-head"))
    db.add(TaskNode(task_id=1, node_id=11, role="worker", node_rank=1, container_name="t1-worker"))
    db.commit()

    async def fake_ps(node, project):
        if node.id == 11:
            return {"containers": [{"name": "t1-worker", "state": "running", "health": "unhealthy"}]}
        return {"containers": [{"name": "t1-head", "state": "running", "health": "healthy"}]}

    monkeypatch.setattr("app.services.agent_client.compose_ps", fake_ps)
    monkeypatch.setattr("app.services.task_monitor.SessionLocal", lambda: db)
    await task_monitor._check_task(1)
    t = db.get(Task, 1)
    assert t.status == "running"   # worker unhealthy 不影响任务
    assert t.health == "healthy"   # 健康按 head 聚合
    db.close()
