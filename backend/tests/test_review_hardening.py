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
