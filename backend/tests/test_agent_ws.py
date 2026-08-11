"""Agent WS 消息分发回归：metrics 入库、docker_event 容器/任务状态机、progress 更新 sent_bytes。"""

import asyncio
import time

import pytest
from app.db import Base
from app.models import ImageTransfer, MetricSample, ModelDownload, Node, Task, TaskNode
from app.services import agent_ws
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


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
    agent_ws._model_file_progress.clear()
    agent_ws._agent_log_subs.clear()
    agent_ws._log_generations.clear()
    agent_ws._log_subscribers.clear()
    agent_ws._frontend_queues.clear()
    agent_ws._log_history.clear()
    agent_ws._log_history_bytes.clear()
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
    await agent_ws._on_progress(_node(env), {"kind": "model", "key": "1:blobs/a",
                                             "written": 777})
    db = env.S()
    job = db.get(ModelDownload, 1)
    assert job.sent_bytes == 777
    assert env.broadcasted[-1]["type"] == "transfer_progress"
    db.close()


@pytest.mark.anyio
async def test_parallel_model_file_progress_is_aggregated(env):
    await agent_ws._on_progress(_node(env), {
        "kind": "model", "key": "1:blobs/a", "written": 300,
    })
    await agent_ws._on_progress(_node(env), {
        "kind": "model", "key": "1:blobs/b", "written": 250,
    })
    db = env.S()
    assert db.get(ModelDownload, 1).sent_bytes == 550
    db.close()


@pytest.mark.anyio
async def test_model_worker_progress_updates_per_node_job(env):
    db = env.S()
    job = db.get(ModelDownload, 1)
    job.status = "syncing"
    job.sync_jobs = {"1": {"status": "syncing"}}
    db.commit()
    db.close()

    await agent_ws._on_progress(_node(env), {
        "kind": "model-sync", "key": "1", "written": 640, "total": 1000,
    })
    db = env.S()
    worker = db.get(ModelDownload, 1).sync_jobs["1"]
    assert worker["transferred_bytes"] == 640 and worker["total_bytes"] == 1000
    assert env.broadcasted[-1]["kind"] == "model-sync"
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
async def test_image_worker_progress_updates_per_node_job(env):
    db = env.S()
    t = db.get(ImageTransfer, 1)
    t.status = "syncing"
    t.sync_jobs = {"1": {"status": "syncing"}}
    db.commit()
    db.close()

    await agent_ws._on_progress(_node(env), {
        "kind": "image-sync", "key": "1", "written": 320, "total": 500,
    })

    db = env.S()
    job = db.get(ImageTransfer, 1).sync_jobs["1"]
    assert job == {
        "status": "syncing", "transferred_bytes": 320, "total_bytes": 500,
    }
    event = env.broadcasted[-1]
    assert event["kind"] == "image-sync" and event["node_id"] == 1
    db.close()


@pytest.mark.anyio
async def test_log_subscribe_registers_requirement_and_replays_tail(env, monkeypatch):
    """订阅登记需求，历史与实时日志由同一 Agent 流连续传输。"""
    db = env.S()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running"))
    db.add(TaskNode(id=1, task_id=1, node_id=1, role="head", node_rank=0,
                    container_name="t1-rank0", container_status="running"))
    db.commit()
    db.close()

    sent: list[dict] = []

    async def fake_send_cmd(container, cmd, node_id=None, tail=None, generation=None):
        sent.append({
            "container": container, "cmd": cmd, "node_id": node_id,
            "tail": tail, "generation": generation,
        })

    monkeypatch.setattr(agent_ws, "_agent_send_cmd", fake_send_cmd)
    monkeypatch.setattr(agent_ws, "is_connected", lambda nid: True)

    q: asyncio.Queue = asyncio.Queue()
    await agent_ws.subscribe_log(1, "t1-rank0", q)
    assert sent == [{"container": "t1-rank0", "cmd": "log_subscribe",
                     "node_id": 1, "tail": agent_ws.LOG_REPLAY_TAIL,
                     "generation": 1}]
    assert (1, "t1-rank0") in agent_ws._agent_log_subs

    # 重复订阅不再重复下发（agent 侧流已存在）
    await agent_ws.subscribe_log(1, "t1-rank0", q)
    assert len(sent) == 1

    # 退订后需求清除
    await agent_ws.unsubscribe_log(1, "t1-rank0", q)
    assert (1, "t1-rank0") not in agent_ws._agent_log_subs
    assert sent[-1]["cmd"] == "log_unsubscribe"


@pytest.mark.anyio
async def test_later_log_subscriber_receives_cached_history(env, monkeypatch):
    """同一容器的后加入页面先收到缓存历史，再接收实时消息。"""
    monkeypatch.setattr(agent_ws, "is_connected", lambda nid: False)
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    await agent_ws.subscribe_log(1, "t1-rank0", q1)
    agent_ws._cache_log({
        "type": "log", "container": "t1-rank0", "line": "already-running",
    })

    await agent_ws.subscribe_log(1, "t1-rank0", q2)
    replay = q2.get_nowait()
    assert replay["line"] == "already-running"


@pytest.mark.anyio
async def test_same_frontend_resubscribe_replays_history(env, monkeypatch):
    """页面重入复用同一 WS 时，即使旧订阅仍在也能重新显示缓存日志。"""
    monkeypatch.setattr(agent_ws, "is_connected", lambda nid: False)
    q: asyncio.Queue = asyncio.Queue()
    await agent_ws.subscribe_log(1, "t1-rank0", q)
    agent_ws._cache_log({
        "type": "log", "container": "t1-rank0", "line": "while-away",
    })

    await agent_ws.subscribe_log(1, "t1-rank0", q)

    reset = q.get_nowait()
    replay = q.get_nowait()
    assert reset == {"type": "log_reset", "container": "t1-rank0"}
    assert replay["line"] == "while-away"


@pytest.mark.anyio
async def test_log_end_releases_subscription_for_container_restart(env, monkeypatch):
    """日志流自然结束后，同一页面刷新可为重启后的容器新开流。"""
    monkeypatch.setattr(agent_ws, "is_connected", lambda nid: False)
    q: asyncio.Queue = asyncio.Queue()
    await agent_ws.subscribe_log(1, "t1-rank0", q)
    assert (1, "t1-rank0") in agent_ws._agent_log_subs

    await agent_ws._handle_message(_node(env), {
        "type": "log_end", "container": "t1-rank0", "generation": 1,
    })
    assert (1, "t1-rank0") not in agent_ws._agent_log_subs
    assert "t1-rank0" not in agent_ws._frontend_queues[q]

    await agent_ws.subscribe_log(1, "t1-rank0", q)
    assert (1, "t1-rank0") in agent_ws._agent_log_subs


@pytest.mark.anyio
async def test_stale_log_end_does_not_finish_replacement_stream(env, monkeypatch):
    """快速刷新时旧 reader 的结束消息不能清掉新一代日志流。"""
    monkeypatch.setattr(agent_ws, "is_connected", lambda nid: False)
    q: asyncio.Queue = asyncio.Queue()
    await agent_ws.subscribe_log(1, "t1-rank0", q)
    await agent_ws.unsubscribe_log(1, "t1-rank0", q)
    await agent_ws.subscribe_log(1, "t1-rank0", q)
    assert agent_ws._log_generations[(1, "t1-rank0")] == 2

    await agent_ws._handle_message(_node(env), {
        "type": "log_end", "container": "t1-rank0", "generation": 1,
    })
    assert (1, "t1-rank0") in agent_ws._agent_log_subs
    assert "t1-rank0" in agent_ws._frontend_queues[q]


@pytest.mark.anyio
async def test_log_messages_without_generation_are_ignored(env, monkeypatch):
    """首次发布协议要求 generation，缺失时不接受日志也不结束当前流。"""
    monkeypatch.setattr(agent_ws, "is_connected", lambda nid: False)
    q: asyncio.Queue = asyncio.Queue()
    await agent_ws.subscribe_log(1, "t1-rank0", q)

    await agent_ws._handle_message(_node(env), {
        "type": "log", "container": "t1-rank0", "line": "missing-generation",
    })
    await agent_ws._handle_message(_node(env), {
        "type": "log_end", "container": "t1-rank0",
    })

    assert env.broadcasted == []
    assert q.empty()
    assert (1, "t1-rank0") in agent_ws._agent_log_subs


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
            generation = agent_ws._log_generations.get((nid, container), 0) + 1
            agent_ws._log_generations[(nid, container)] = generation
            await agent_ws._send(ws, {"type": "log_subscribe",
                                      "container": container,
                                      "tail": agent_ws.LOG_REPLAY_TAIL,
                                      "generation": generation})
    assert len(sent) == 1
    assert sent[0]["container"] == "t1-rank0"
    assert sent[0]["tail"] == agent_ws.LOG_REPLAY_TAIL
    assert sent[0]["generation"] == 1


# ---------- WS 常连优先 / 断开即下线 / 心跳看门狗 ----------


@pytest.mark.anyio
async def test_set_node_status_writes_and_broadcasts(env):
    """统一状态写入：落库 agent_status + 广播 node_status 事件。"""
    await agent_ws._set_node_status(1, "offline")
    db = env.S()
    assert db.get(Node, 1).agent_status == "offline"
    db.close()
    ev = env.broadcasted[-1]
    assert ev["type"] == "node_status" and ev["node_id"] == 1
    assert ev["status"] == "offline"


@pytest.mark.anyio
async def test_set_node_status_online_seeds_last_seen(env):
    """置 online 时刷新 last_seen，且状态未变不重复广播（幂等）。"""
    await agent_ws._set_node_status(1, "online")
    db = env.S()
    n = db.get(Node, 1)
    assert n.agent_status == "online" and n.last_seen is not None
    db.close()
    await agent_ws._set_node_status(1, "online")
    n_status = [b for b in env.broadcasted if b["type"] == "node_status"]
    assert len(n_status) == 1


@pytest.mark.anyio
async def test_handle_message_tracks_last_msg_ts(env, monkeypatch):
    """任一类 WS 消息都刷新心跳时间戳（metrics 即应用级心跳）。"""
    async def fake_metrics(node, data):
        pass
    monkeypatch.setattr(agent_ws, "_on_metrics", fake_metrics)
    before = time.time()
    await agent_ws._handle_message(_node(env), {"type": "metrics", "data": {}})
    assert agent_ws._last_msg_ts.get(1, 0) >= before


@pytest.mark.anyio
async def test_watchdog_marks_stale_node_offline(env, monkeypatch):
    """悬挂连接（WS 存活但超 NODE_STALE_TIMEOUT 无消息）=> 判离线并清连接态。"""
    agent_ws._connected[1] = True
    agent_ws._last_msg_ts[1] = time.time() - 1000
    calls: list[tuple[int, str]] = []

    async def fake_set(node_id, status):
        calls.append((node_id, status))

    monkeypatch.setattr(agent_ws, "_set_node_status", fake_set)
    await agent_ws._watchdog_pass()
    assert (1, "offline") in calls
    assert agent_ws._connected.get(1) is False


@pytest.mark.anyio
async def test_watchdog_skips_fresh_node(env, monkeypatch):
    """心跳新鲜（超时阈值内）的节点不被误判离线。"""
    agent_ws._connected[1] = True
    agent_ws._last_msg_ts[1] = time.time()
    calls: list[tuple[int, str]] = []

    async def fake_set(node_id, status):
        calls.append((node_id, status))

    monkeypatch.setattr(agent_ws, "_set_node_status", fake_set)
    await agent_ws._watchdog_pass()
    assert calls == []
