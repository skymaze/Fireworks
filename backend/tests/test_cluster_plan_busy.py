"""集群发布 plan：节点占用（被 active 任务承载）标记回归。

create_task 对节点的互斥判定（published/running/paused）下发给发布页，
使 frontend 能置灰占用节点并自动改选空闲节点（买票式选座）。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Cluster, ClusterNode, Node, Task, TaskNode
from app.routers.clusters import cluster_plan


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for i in (1, 2, 3, 4):
        db.add(Node(id=i, name=f"n{i}", ip=f"192.0.2.{i}"))
    db.add(Cluster(id=1, name="c1", network_type="roce"))
    for i, idx in ((1, 1), (2, 2), (3, 3)):
        db.add(ClusterNode(cluster_id=1, node_id=i, net_index=idx))
    db.commit()
    yield db
    db.close()


def _add_task(db, task_id, name, status, node_ids):
    db.add(Task(id=task_id, name=name, status=status, recipe_id=1,
                cluster_id=1, variables={}))
    for nid in node_ids:
        db.add(TaskNode(task_id=task_id, node_id=nid, role="worker", node_rank=1))
    db.commit()


def test_plan_marks_nodes_busy_by_active_tasks(db):
    # 节点 1 被 running 任务占用，节点 2 被 published 占用，节点 3 空闲
    _add_task(db, 1, "task-a", "running", [1])
    _add_task(db, 2, "task-b", "published", [2])
    nodes = cluster_plan(1, db)["nodes"]
    by_id = {n["node_id"]: n for n in nodes}
    assert by_id[1]["busy"] is True and by_id[1]["busy_task"] == "task-a"
    assert by_id[2]["busy"] is True and by_id[2]["busy_task"] == "task-b"
    assert by_id[3]["busy"] is False and by_id[3]["busy_task"] is None


def test_plan_free_after_task_ends(db):
    # 已停止/已删除的任务不再占用节点
    _add_task(db, 1, "task-old", "stopped", [1])
    _add_task(db, 2, "task-err", "error", [2])
    nodes = cluster_plan(1, db)["nodes"]
    by_id = {n["node_id"]: n for n in nodes}
    assert by_id[1]["busy"] is False
    assert by_id[2]["busy"] is False
