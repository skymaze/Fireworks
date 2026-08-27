"""审查加固回归测试（R1：生产兼容性安全修复）。

覆盖：
- revision 校验（路径越级/字符集）入口 + 服务层
- 模型缓存大小统计排除 .part.N 分片残留
- 镜像 blob 下载 401 无限重认证死循环（上限终止）
- 删除进行中任务/传输前先取消后台调度（防孤儿线程双写）
- 删除本地缓存时进行中任务拒绝（MODEL_BUSY）
- task 日志 tail 参数钳制

全部为内存 SQLite / 纯函数级，不触碰真实数据库与网络。
"""

import contextlib
import socket

import pytest
from app.db import Base
from app.models import ImageTransfer, ModelDownload, Node, Task, TaskNode
from app.routers import images as images_router
from app.routers import models as models_router
from app.routers import tasks as tasks_router
from app.services import model_manager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ---------- revision 校验 ----------


def test_validate_revision_allows_branches_tags_and_commits():
    for ok in ["main", "feature/xx", "v1.2.3", "a" * 40, "refs/heads/main",
               "model--tuning", "a.b", "main-2"]:
        assert model_manager.validate_revision(ok) == ok


def test_validate_revision_rejects_path_traversal_and_junk():
    for bad in ["../../etc", "a/../../b", "/etc", "main/", "..", "a b", "a;rm -rf /",
                "a\\b", "'quote'", '"dq"', "a" * 129, "a..b"]:
        with pytest.raises(ValueError):
            model_manager.validate_revision(bad)


def test_validate_revision_empty_defaults_to_main():
    assert model_manager.validate_revision("") == "main"
    assert model_manager.validate_revision(None) == "main"


def test_download_sync_rejects_invalid_revision_before_network(monkeypatch):
    """非法 revision 在 _download_sync 入口即抛错，绝不触网/写盘。"""
    called = {}

    def boom(*a, **k):
        called["hit"] = True
        raise AssertionError("不应触网")

    monkeypatch.setattr(model_manager, "get_hf_settings", boom)
    with pytest.raises(ValueError):
        model_manager._download_sync("org/repo", "../../etc")


# ---------- .part.N 残留过滤（local_model_size） ----------


def test_local_model_size_excludes_part_chunks(monkeypatch, tmp_path):
    monkeypatch.setattr(model_manager.config, "MODEL_CACHE_DIR", str(tmp_path))
    d = tmp_path / "models--org--repo" / "blobs"
    d.mkdir(parents=True)
    # 完整 blob：计入
    (d / ("a" * 64)).write_bytes(b"x" * 100)
    # 下载中临时文件：跳过
    (d / ".incomplete").write_bytes(b"y" * 40)
    (d / ".lock").write_bytes(b"")
    # 普通文件（无分片/临时后缀）：计入
    (d / ("b" * 64)).write_bytes(b"z" * 20)
    (d / ("c" * 64)).write_bytes(b"w" * 5)
    # 中断残留分片（形如 .<hash>.incomplete.part.N）：不计入
    (d / ".deadbeef.incomplete.part.0").write_bytes(b"v" * 33)
    (d / ".deadbeef.incomplete.part.1").write_bytes(b"v" * 33)

    total = model_manager.local_model_size("org/repo")
    # 只统计完整/普通文件：100 + 20 + 5
    assert total == 125


# ---------- 镜像 blob 401 无限重认证死循环 ----------


class _FakeBlobResp:
    def __init__(self):
        self.status_code = 401
        self.headers = {
            "www-authenticate": "Bearer realm=\"https://reg.example.com/token\"",
        }

    def raise_for_status(self):
        import httpx
        raise httpx.HTTPStatusError(
            "401", request=httpx.Request("GET", "https://reg.example.com/v2/x"),
            response=httpx.Response(401),
        )


def test_registry_blob_file_stops_after_401_retries(monkeypatch, tmp_path):
    calls = {"n": 0}

    class FakeClient:
        def stream(self, *a, **k):
            calls["n"] += 1
            return contextlib.nullcontext(_FakeBlobResp())

    from app.services import image_manager as img_mgr

    monkeypatch.setattr(img_mgr, "_token_from_challenge", lambda c, ch: "refreshed-token")

    with pytest.raises(RuntimeError, match="401"):
        img_mgr._registry_blob_file(
            FakeClient(), "reg.example.com", "lib/x", "sha256:abc",
            "initial-token", tmp_path / "blob",
        )
    # 401 分支每次重试都在 attempts 计数内：共 5 次后抛出，绝不死循环
    assert calls["n"] == 5


# ---------- 共享路径 / 令牌校验（M5） ----------


def test_validate_share_path_security():
    from app.services import peer_transfer

    ok = peer_transfer.validate_share_path("/api/image/share/sha256:abc")
    assert ok.startswith("/api/image/share/")
    for bad in ["@evil.example:80/x", "x", "//a/../b", "/a/../b", "/a/./b",
                "/a b", "/a\\b", "/a@b", "/a\nb", "/a/b/../../c", "http://x/", "/'q'"]:
        with pytest.raises(ValueError):
            peer_transfer.validate_share_path(bad)


def test_validate_share_token_security():
    from app.services import peer_transfer

    peer_transfer.validate_share_token("short-token")
    for bad in ["a\r\nX-Injected: 1", "\0", "x" * 513]:
        with pytest.raises(ValueError):
            peer_transfer.validate_share_token(bad)


# ---------- 节点 SSH 用户名 / 地址校验（H5） ----------


def test_node_create_validates_username_and_ip():
    from app.schemas import NodeCreate
    from pydantic import ValidationError

    NodeCreate(name="n1", ip="192.0.168.10", ssh_username="root")
    NodeCreate(name="n2", ip="2001:db8::1", ssh_username="foo.bar-baz_1")
    # 默认值合法：root + IP
    NodeCreate(name="n3", ip="10.0.0.1")
    for bad_ip in ["192.168.1.1; rm -rf /", "a b", "x@y", "10.0.0.1/../x", ""]:
        with pytest.raises(ValidationError):
            NodeCreate(name="n", ip=bad_ip)
    for bad_user in ["root; id", "a/b", "-x", "1abc", "a b", "root\""]:
        with pytest.raises(ValidationError):
            NodeCreate(name="n", ip="10.0.0.1", ssh_username=bad_user)


def test_uninstall_rejects_invalid_username_without_ssh(monkeypatch):
    """非法 SSH 用户名在发起 SSH 之前即被拒绝（防御 Shell 注入）。"""
    from app.models import Node
    from app.services import deploy_agent

    node = Node(id=1, name="n1", ip="10.0.0.1", ssh_username="root; id")
    monkeypatch.setattr(
        deploy_agent.ssh_client, "connect",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 SSH")),
    )
    ok, msg = deploy_agent._uninstall_sync(node)
    assert ok is False and "非法" in msg


# ---------- 任务状态机加固（H6） ----------


def _task_db():
    db = _mem_db()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running"))
    db.add(TaskNode(id=1, task_id=1, node_id=10, role="head", node_rank=0,
                    container_name="t1-rank0"))
    db.add(Node(id=10, name="n1", ip="192.0.2.10"))
    db.commit()
    return db


@pytest.mark.anyio
async def test_task_action_resume_on_stopped_rejected(monkeypatch):
    """对已停止任务 resume 返回 409，不再制造「无容器的 running」卡死状态。"""
    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest
    from fastapi import HTTPException

    db = _task_db()
    db.query(Task).filter_by(id=1).update({"status": "stopped"})
    db.commit()
    with pytest.raises(HTTPException) as ei:
        await tasks_router.task_action(1, TaskActionRequest(action="resume"), db)
    assert ei.value.status_code == 409
    db.close()


@pytest.mark.anyio
async def test_task_action_stop_on_error_allowed(monkeypatch):
    """error 状态任务仍可 stop（前端在 error 显示停止按钮），不应 409。"""
    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest

    db = _task_db()
    db.query(Task).filter_by(id=1).update({"status": "error"})
    db.commit()

    async def fake_action(node, project, action):
        assert action == "stop"
        return None

    async def fake_bc(*a, **k):
        return None

    monkeypatch.setattr("app.services.agent_client.compose_action", fake_action)
    monkeypatch.setattr("app.routers.tasks.agent_ws.broadcast", fake_bc)
    r = await tasks_router.task_action(1, TaskActionRequest(action="stop"), db)
    assert r["status"] == "stopped"
    db.close()


@pytest.mark.anyio
async def test_task_action_pause_running_ok(monkeypatch):
    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest

    db = _task_db()
    calls = []

    async def fake_action(node, name, action):
        calls.append((node.id, name, action))

    monkeypatch.setattr("app.services.agent_client.container_action", fake_action)
    async def _fake_broadcast(*a, **k):
        return None

    monkeypatch.setattr("app.routers.tasks.agent_ws.broadcast", _fake_broadcast)
    r = await tasks_router.task_action(1, TaskActionRequest(action="pause"), db)
    assert r["status"] == "paused"
    assert calls == [(10, "t1-rank0", "pause")]
    assert db.get(Task, 1).status == "paused"
    db.close()


@pytest.mark.anyio
async def test_task_action_stop_all_managed_nodes_failed_sets_error(monkeypatch):
    """全部管理节点 down 失败的 stop 置 error，而不是虚报 stopped。"""
    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest

    db = _task_db()

    async def fake_action(node, project, action):
        raise RuntimeError("agent 不可达")

    monkeypatch.setattr("app.services.agent_client.compose_action", fake_action)
    async def _fake_broadcast(*a, **k):
        return None

    monkeypatch.setattr("app.routers.tasks.agent_ws.broadcast", _fake_broadcast)
    r = await tasks_router.task_action(1, TaskActionRequest(action="stop"), db)
    assert r["status"] == "error"
    assert db.get(Task, 1).status == "error"
    db.close()


@pytest.mark.anyio
async def test_task_action_stop_partial_failure_keeps_stopped(monkeypatch):
    """部分节点 down 失败（如未启动节点）仍可停到 stopped，errors 透出提示。"""
    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest

    db = _task_db()
    # error 状态下二节点任务：一个 compose stop 成功、一个失败 -> 部分失败仍置 stopped
    db.add(TaskNode(id=2, task_id=1, node_id=11, role="worker", node_rank=1))
    db.add(Node(id=11, name="n2", ip="192.0.2.11"))
    db.query(Task).filter_by(id=1).update({"status": "running"})
    db.commit()
    stopped = []

    async def fake_action(node, project, action):
        if node.id == 10:
            stopped.append((node.id, action))
            return None
        raise RuntimeError("agent 不可达")

    monkeypatch.setattr("app.services.agent_client.compose_action", fake_action)
    async def _fake_broadcast(*a, **k):
        return None

    monkeypatch.setattr("app.routers.tasks.agent_ws.broadcast", _fake_broadcast)
    r = await tasks_router.task_action(1, TaskActionRequest(action="stop"), db)
    assert r["status"] == "stopped"
    assert stopped == [(10, "stop")]
    db.close()


@pytest.mark.anyio
async def test_task_action_restart_running_ok(monkeypatch):
    """running 任务可重启（docker compose restart），保持 running 且不重建。"""
    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest

    db = _task_db()
    calls = []

    async def fake_action(node, project, action):
        calls.append((node.id, project, action))
        return None

    async def fake_bc(*a, **k):
        return None

    monkeypatch.setattr("app.services.agent_client.compose_action", fake_action)
    monkeypatch.setattr("app.routers.tasks.agent_ws.broadcast", fake_bc)
    r = await tasks_router.task_action(1, TaskActionRequest(action="restart"), db)
    assert r["status"] == "running"
    assert calls == [(10, "t1", "restart")]
    db.close()


@pytest.mark.anyio
async def test_task_action_restart_stopped_rejected(monkeypatch):
    """stopped 任务不可 restart（应使用 start），返回 409。"""
    from fastapi import HTTPException

    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest

    db = _task_db()
    db.query(Task).filter_by(id=1).update({"status": "stopped"})
    db.commit()
    with pytest.raises(HTTPException) as ei:
        await tasks_router.task_action(1, TaskActionRequest(action="restart"), db)
    assert ei.value.status_code == 409
    db.close()


@pytest.mark.anyio
async def test_task_action_start_stopped_ok(monkeypatch):
    """stopped 任务可启动（docker compose start 复用容器），恢复到 running。"""
    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest

    db = _task_db()
    db.query(Task).filter_by(id=1).update({"status": "stopped"})
    db.commit()
    calls = []

    async def fake_action(node, project, action):
        calls.append((node.id, project, action))
        return None

    async def fake_bc(*a, **k):
        return None

    monkeypatch.setattr("app.services.agent_client.compose_action", fake_action)
    monkeypatch.setattr("app.routers.tasks.agent_ws.broadcast", fake_bc)
    r = await tasks_router.task_action(1, TaskActionRequest(action="start"), db)
    assert r["status"] == "running"
    assert calls == [(10, "t1", "start")]
    db.close()


@pytest.mark.anyio
async def test_task_action_start_falls_back_to_compose_up(monkeypatch):
    """start 时容器已被清理（compose start 失败）→ 回退 compose up 用发布配置重建。"""
    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest

    db = _task_db()
    db.query(Task).filter_by(id=1).update({
        "status": "stopped",
        "rendered": {
            "nodes": {
                "10": {"project": "t1", "role": "head",
                       "compose_yaml": "services:\n  x: {}\n", "env": {}},
            },
        },
    })
    db.commit()
    seen = []

    async def fake_action(node, project, action):
        raise RuntimeError("no container to start")

    async def fake_up(node, project, compose_yaml, env):
        seen.append(("up", project))
        return {"ok": True}

    async def fake_bc(*a, **k):
        return None

    monkeypatch.setattr("app.services.agent_client.compose_action", fake_action)
    monkeypatch.setattr("app.services.agent_client.compose_up", fake_up)
    monkeypatch.setattr("app.routers.tasks.agent_ws.broadcast", fake_bc)
    r = await tasks_router.task_action(1, TaskActionRequest(action="start"), db)
    assert r["status"] == "running"
    assert seen == [("up", "t1")]
    db.close()


@pytest.mark.anyio
async def test_task_action_start_no_container_rejected(monkeypatch):
    """任务从未成功启动过容器（无容器名）时 start 返回 409（提示重新发布）。"""
    from fastapi import HTTPException

    from app.routers import tasks as tasks_router
    from app.schemas import TaskActionRequest

    db = _task_db()
    db.query(Task).filter_by(id=1).update({"status": "stopped"})
    db.query(TaskNode).filter_by(task_id=1).update({"container_name": None})
    db.commit()
    with pytest.raises(HTTPException) as ei:
        await tasks_router.task_action(1, TaskActionRequest(action="start"), db)
    assert ei.value.status_code == 409
    assert "重新发布" in str(ei.value.detail)
    db.close()


# ---------- 推理统计：无效区间与虚假峰值防御 ----------


def _sample(task_id, node_id, ts, gen, prefill=0, reqs=0):
    from app.models import InferenceSample

    return InferenceSample(
        id=0, task_id=task_id, node_id=node_id, ts=ts,
        data={
            "generation_tokens_total": gen,
            "prompt_tokens_total": prefill,
            "request_success_total": reqs,
        },
    )


def test_inference_aggregation_skips_zero_duration_intervals():
    """ts 相同/倒流的累计快照被跳过，不产生天文速率峰值。"""
    from app.services import inference_aggregation as agg

    rows = [
        _sample(1, 1, 100.0, gen=100),
        _sample(1, 1, 100.0, gen=200),   # 相同 ts：应被跳过
        _sample(1, 1, 101.0, gen=300),
        _sample(1, 1, 100.5, gen=400),   # 与上一条倒流（101 -> 100.5）：应被跳过
        _sample(1, 1, 106.0, gen=500),
    ]
    r = agg.aggregate_inference_samples(
        rows, from_ts=95.0, to_ts=200.0, max_points=10, task_names={1: "t"},
    )
    s = r["summary"]
    assert s["decode_peak_tokens_per_sec"] is None or s["decode_peak_tokens_per_sec"] < 1000


def test_inference_aggregation_clamps_tiny_duration():
    """极小正间隔速率被 0.1s 下限钳制，不会出现 1e-6 导致的百万级峰值。"""
    from app.services import inference_aggregation as agg

    rows = [
        _sample(1, 1, 100.0, gen=100),
        _sample(1, 1, 100.001, gen=101),  # 1 token 在 1ms 内
        _sample(1, 1, 105.0, gen=106),
    ]
    r = agg.aggregate_inference_samples(
        rows, from_ts=99.0, to_ts=200.0, max_points=10, task_names={1: "t"},
    )
    peak = r["summary"]["decode_peak_tokens_per_sec"]
    assert peak is not None and peak < 1000.0


# ---------- 下载设置变更重启看门狗 ----------


def test_restart_watchdog_starts_download_when_no_thread(monkeypatch):
    """join 目标不存在时看门狗直接按新设置重启下载（同步路径）。"""
    started = []
    monkeypatch.setattr(
        model_manager, "_start_local_download",
        lambda j, r, v: started.append((j, r, v)),
    )
    model_manager._download_threads.clear()
    model_manager._schedule_restart_watchdog(1, "org/repo", "main")
    assert started == [(1, "org/repo", "main")]


def test_restart_watchdog_resumes_after_stuck_thread_exits(monkeypatch):
    """旧线程卡住退出后，看门狗（异步守护）用一致性校验重启任务。"""
    import time as _time

    started = []
    monkeypatch.setattr(
        model_manager, "_start_local_download",
        lambda j, r, v: started.append((j, r, v)),
    )

    class _FakeThread:
        def join(self, timeout=None):  # 立即"退出"
            return None

    fake = _FakeThread()
    model_manager._download_threads[7] = fake
    model_manager._schedule_restart_watchdog(7, "org/repo", "main")
    # 守护线程异步执行：轮询等待至多 2s
    deadline = _time.monotonic() + 2.0
    while not started and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert started == [(7, "org/repo", "main")]
    assert 7 not in model_manager._download_threads
    model_manager._download_threads.clear()


# ---------- 删除进行中任务前先取消（防孤儿线程） ----------


def _mem_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.mark.anyio
async def test_delete_transfer_active_cancels_then_deletes(monkeypatch):
    db = _mem_db()
    db.add(ImageTransfer(id=7, image="example/img:1", status="pulling"))
    db.commit()
    cancelled = {}

    async def fake_cancel(job_id):
        cancelled["id"] = job_id
        t = db.get(ImageTransfer, job_id)
        if t:
            t.status = "cancelled"
            db.commit()

    monkeypatch.setattr("app.services.image_manager.cancel_image_transfer", fake_cancel)
    # 需要模块内的 session 提交一致；直接替换 commit 语义即可
    result = await images_router.delete_transfer(7, db=db)
    assert result["ok"] is True
    assert cancelled == {"id": 7}
    assert db.get(ImageTransfer, 7) is None
    db.close()


@pytest.mark.anyio
async def test_delete_download_active_cancels_then_deletes(monkeypatch):
    db = _mem_db()
    db.add(ModelDownload(id=9, repo="org/repo", revision="main", status="downloading"))
    db.commit()
    cancelled = {}

    async def fake_cancel(job_id):
        cancelled["id"] = job_id
        j = db.get(ModelDownload, job_id)
        if j:
            j.status = "cancelled"
            db.commit()

    monkeypatch.setattr("app.services.model_manager.cancel_download", fake_cancel)
    result = await models_router.delete_download(9, cleanup=0, db=db)
    assert result["ok"] is True
    assert cancelled == {"id": 9}
    assert db.get(ModelDownload, 9) is None
    db.close()


@pytest.mark.anyio
async def test_batch_delete_downloads_active_cancels_then_deletes(monkeypatch):
    db = _mem_db()
    db.add(ModelDownload(id=1, repo="a/b", revision="main", status="syncing"))
    db.add(ModelDownload(id=2, repo="c/d", revision="main", status="completed"))
    db.commit()
    cancelled = []

    async def fake_cancel(job_id):
        cancelled.append(job_id)
        j = db.get(ModelDownload, job_id)
        if j:
            j.status = "cancelled"
            db.commit()

    monkeypatch.setattr("app.services.model_manager.cancel_download", fake_cancel)
    result = await models_router.batch_delete_downloads(
        models_router.BatchDeleteRequest(ids=[1, 2]), db=db)
    assert result["ok"] is True and result["deleted"] == 2
    assert cancelled == [1]
    assert db.get(ModelDownload, 1) is None and db.get(ModelDownload, 2) is None
    db.close()


def test_delete_local_model_rejects_active_download():
    db = _mem_db()
    db.add(ModelDownload(id=3, repo="org/repo", revision="main", status="sending"))
    db.commit()
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        models_router.delete_local_model("org/repo", db=db)
    assert ei.value.status_code == 409
    db.close()


# ---------- task 日志 tail 钳制 ----------


@pytest.mark.anyio
async def test_task_logs_tail_clamped(monkeypatch):
    db = _mem_db()
    db.add(Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running"))
    db.add(Node(id=2, name="n1", ip="192.0.2.2"))
    db.add(TaskNode(task_id=1, node_id=2, role="head", container_name="t1-node-2-xxx"))
    db.commit()
    seen = {}

    async def fake_logs(node, container, tail):
        seen["tail"] = tail
        return ["line"]

    monkeypatch.setattr("app.services.agent_client.container_logs", fake_logs)
    await tasks_router.task_logs(1, 2, tail=999999, db=db)
    assert seen["tail"] == 5000
    await tasks_router.task_logs(1, 2, tail=-5, db=db)
    assert seen["tail"] == 1
    await tasks_router.task_logs(1, 2, tail=300, db=db)
    assert seen["tail"] == 300
    db.close()


# ---------- ssh exec 总超时（挂死命令终止） ----------


def test_ssh_exec_enforces_total_deadline(monkeypatch):
    """远端进程存活但静默时，exec 按 timeout 终止并抛错（不再无限挂起）。"""
    from app.services import ssh_client

    class FakeChan:
        def __init__(self):
            self._sock = socket.socketpair()[0]

        def fileno(self):
            return self._sock.fileno()

        def recv_ready(self):
            return False

        def recv_stderr_ready(self):
            return False

        def exit_status_ready(self):
            return False

        def close(self):
            try:
                self._sock.close()
            except OSError:
                pass

        def recv_exit_status(self):
            return -1

    class FakeStdStream:
        def __init__(self, chan):
            self.channel = chan

        def write(self, *a):
            pass

        def flush(self):
            pass

    chan = FakeChan()
    stdin = FakeStdStream(chan)

    class FakeClient:
        def exec_command(self, command, timeout=None):
            assert timeout is not None
            return stdin, FakeStdStream(chan), None

    start = __import__("time").monotonic()
    with pytest.raises(TimeoutError, match="超时"):
        ssh_client.exec(FakeClient(), "sleep 1000", timeout=1)
    assert __import__("time").monotonic() - start < 5
    chan.close()
