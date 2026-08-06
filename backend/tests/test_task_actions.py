"""任务操作竞态回归：双删除/删除+停止不再 500（StaleDataError），容器消失后日志返回 404。

修复前：并发/重复操作在 task_action 的 db.commit() 抛 StaleDataError -> 500；
task_logs 在 agent 返回 404（容器已停止）时未捕获 -> 500。
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Task, TaskNode
from app.routers.tasks import task_action, task_logs
from app.schemas import TaskActionRequest


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    s.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running"))
    s.commit()
    yield s
    s.close()


@pytest.mark.anyio
async def test_delete_then_delete_returns_404(db):
    """第一次删除成功后，再次删除同一任务返回 404（修复前第二次操作会 500）。"""
    r1 = await task_action(1, TaskActionRequest(action="delete"), db)
    assert r1["ok"] is True
    with pytest.raises(HTTPException) as e:
        await task_action(1, TaskActionRequest(action="delete"), db)
    assert e.value.status_code == 404


@pytest.mark.anyio
async def test_stale_action_race_returns_409(db):
    """删除竞态：会话2 持有已被会话1 删除的任务，提交时 StaleDataError -> 409 而非 500。"""
    engine = db.get_bind()
    S = sessionmaker(bind=engine)
    db2 = S()
    try:
        stale = db2.get(Task, 1)  # 会话2 先加载任务（模拟并发请求的陈旧读取）
        assert stale is not None
        # 会话1 删除任务
        r1 = await task_action(1, TaskActionRequest(action="delete"), db)
        assert r1["ok"] is True
        # 会话2 用陈旧对象执行 stop -> 提交时 StaleDataError -> 应转 409
        with pytest.raises(HTTPException) as e:
            await task_action(1, TaskActionRequest(action="stop"), db2)
        assert e.value.status_code == 409
        assert "刷新" in str(e.value.detail)
    finally:
        db2.close()


@pytest.mark.anyio
async def test_task_logs_container_gone_returns_404(db, monkeypatch):
    """容器已停止/删除（agent 404）时，日志接口返回 404 而非 500。"""
    from app.models import Node

    db.add(Node(id=1, name="n1", ip="192.0.2.1"))
    db.add(TaskNode(task_id=1, node_id=1, role="head", node_rank=0, container_name="t1-rank0"))
    db.commit()

    async def _boom(node, name, tail):
        raise RuntimeError("agent 404: 容器不存在")

    monkeypatch.setattr("app.routers.tasks.agent_client.container_logs", _boom)
    with pytest.raises(HTTPException) as e:
        await task_logs(1, 1, 200, db)
    assert e.value.status_code == 404
    assert "日志不可用" in str(e.value.detail)
