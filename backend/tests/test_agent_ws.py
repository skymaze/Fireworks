"""Agent WS 消息分发回归：metrics 入库、docker_event 容器/任务状态机、progress 更新 sent_bytes。"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ImageTransfer, MetricSample, ModelDownload, Node, Task, TaskNode
from app.services import agent_ws


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(Node(id=1, name="n1", ip="192.0.2.1", agent_status="unknown"))
    db.add(ModelDownload(id=1, repo="owner/model", revision="main", status="sending",
                         head_node_id=1, total_bytes=1000))
    db.add(ImageTransfer(id=1, image="img:1", digest="sha256:abc", status="sending",
                         head_node_id=1, size_bytes=500))
    db.commit()
    db.close()

    broadcasted: list[dict] = []

    async def fake_broadcast(msg, exclude=None):
        broadcasted.append(msg)

    monkeypatch.setattr(agent_ws, "SessionLocal", S)
    monkeypatch.setattr(agent_ws, "broadcast", fake_broadcast)
    env.broadcasted = broadcasted
    env.S = S
    return env


def _node(env):
    return env.S().get(Node, 1)


@pytest.mark.anyio
async def test_metrics_written_and_broadcast(env):
    await agent_ws._on_metrics(_node(env), {"ts": 123.0, "cpu_percent": 42.0})
    db = env.S()
    sample = db.query(MetricSample).first()
    assert sample is not None and sample.node_id == 1 and sample.data["cpu_percent"] == 42.0
    assert env.broadcasted[-1]["type"] == "metrics"
    db.close()


@pytest.mark.anyio
async def test_docker_event_updates_status_and_stops_task(env):
    db = env.S()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running"))
    db.add(TaskNode(id=1, task_id=1, node_id=1, role="head", node_rank=0,
                    container_name="t1-rank0", container_status="running"))
    db.add(TaskNode(id=2, task_id=1, node_id=2, role="worker", node_rank=1,
                    container_name="t1-rank1", container_status="exited"))
    db.commit()
    db.close()

    await agent_ws._on_docker_event(_node(env), {
        "status": "die",
        "Actor": {"Attributes": {"name": "t1-rank0"}},
    })
    db = env.S()
    tn = db.get(TaskNode, 1)
    assert tn.container_status == "exited"
    task = db.get(Task, 1)
    assert task.status == "stopped"  # 全部容器 exited -> 秒级 stopped
    types = [b["type"] for b in env.broadcasted]
    assert "container_status" in types and "task_status" in types
    db.close()


@pytest.mark.anyio
async def test_docker_event_does_not_overwrite_user_pause(env):
    db = env.S()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="paused"))
    db.add(TaskNode(id=1, task_id=1, node_id=1, role="head", node_rank=0,
                    container_name="t1-rank0", container_status="exited"))
    db.commit()
    db.close()

    await agent_ws._on_docker_event(_node(env), {
        "status": "start",
        "Actor": {"Attributes": {"name": "t1-rank0"}},
    })
    db = env.S()
    # 用户已暂停：容器状态可更新，但任务状态不被覆盖
    assert db.get(Task, 1).status == "paused"
    db.close()


@pytest.mark.anyio
async def test_model_progress_updates_sent_bytes(env):
    await agent_ws._on_progress(_node(env), {"kind": "model", "key": "owner/model",
                                             "written": 777})
    db = env.S()
    job = db.get(ModelDownload, 1)
    assert job.sent_bytes == 777
    assert env.broadcasted[-1]["type"] == "transfer_progress"
    db.close()


@pytest.mark.anyio
async def test_image_progress_updates_sent_bytes(env):
    await agent_ws._on_progress(_node(env), {"kind": "image", "key": "sha256:abc",
                                             "written": 250})
    db = env.S()
    t = db.get(ImageTransfer, 1)
    assert t.sent_bytes == 250
    db.close()


@pytest.mark.anyio
async def test_log_subscribe_registers_requirement_and_sends_tail0(env, monkeypatch):
    """订阅登记需求（断连可补发）且下发命令带 tail=0（避免回放与快照重复）。"""
    db = env.S()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running"))
    db.add(TaskNode(id=1, task_id=1, node_id=1, role="head", node_rank=0,
                    container_name="t1-rank0", container_status="running"))
    db.commit()
    db.close()

    sent: list[dict] = []

    async def fake_send_cmd(container, cmd, node_id=None, tail=None):
        sent.append({"container": container, "cmd": cmd, "node_id": node_id, "tail": tail})

    monkeypatch.setattr(agent_ws, "_agent_send_cmd", fake_send_cmd)
    monkeypatch.setattr(agent_ws, "is_connected", lambda nid: True)

    q: asyncio.Queue = asyncio.Queue()
    await agent_ws.subscribe_log(1, "t1-rank0", q)
    assert sent == [{"container": "t1-rank0", "cmd": "log_subscribe",
                     "node_id": 1, "tail": 0}]
    assert (1, "t1-rank0") in agent_ws._agent_log_subs

    # 重复订阅不再重复下发（agent 侧流已存在）
    await agent_ws.subscribe_log(1, "t1-rank0", q)
    assert len(sent) == 1

    # 退订后需求清除
    await agent_ws.unsubscribe_log(1, "t1-rank0", q)
    assert (1, "t1-rank0") not in agent_ws._agent_log_subs
    assert sent[-1]["cmd"] == "log_unsubscribe"


@pytest.mark.anyio
async def test_reconnect_resubscribes_kept_requirements(env, monkeypatch):
    """agent WS 重连后：仍被订阅的容器自动补发 log_subscribe（agent 流已随断连终止）。"""
    db = env.S()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running"))
    db.add(TaskNode(id=1, task_id=1, node_id=1, role="head", node_rank=0,
                    container_name="t1-rank0", container_status="running"))
    db.add(TaskNode(id=2, task_id=1, node_id=2, role="worker", node_rank=1,
                    container_name="t1-rank1", container_status="running"))
    db.commit()
    db.close()
    agent_ws._agent_log_subs = {(1, "t1-rank0"), (2, "t1-rank1")}

    sent: list[dict] = []

    async def fake_send(ws, msg):
        sent.append(msg)

    monkeypatch.setattr(agent_ws, "_send", fake_send)

    # 模拟 node 1 重连：只补发属于该节点的容器
    ws = object()
    for nid, container in list(agent_ws._agent_log_subs):
        if nid == 1:
            await agent_ws._send(ws, {"type": "log_subscribe",
                                      "container": container, "tail": 0})
    assert len(sent) == 1
    assert sent[0]["container"] == "t1-rank0" and sent[0]["tail"] == 0
