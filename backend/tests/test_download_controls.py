"""下载任务 暂停/继续/取消 回归：状态流转 + 线程重启逻辑。

真实下载线程/监控会触网，测试中替换为桩；分片续传与网络行为由实机验证覆盖。
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ModelDownload
from app.services import model_manager


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    """隔离的 service 环境：独立 DB + 桩线程/监控。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(ModelDownload(id=1, repo="test/repo", revision="main",
                         status="downloading", total_bytes=1000))
    db.commit()
    db.close()

    started: list[tuple[int, str, str]] = []

    def fake_start(job_id, repo, revision):
        started.append((job_id, repo, revision))

    async def fake_monitor(job_id):
        pass

    monkeypatch.setattr(model_manager, "SessionLocal", S)
    monkeypatch.setattr(model_manager, "_start_local_download", fake_start)
    monkeypatch.setattr(model_manager, "_monitor_job", fake_monitor)
    # 线程注册表：模拟一个已退出的旧线程（is_alive=False），resume 应启动新线程
    class _DeadThread:
        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    monkeypatch.setitem(model_manager._download_threads, 1, _DeadThread())
    svc = model_manager
    svc.started = started
    return svc


def test_pause_resume_cancel_flow(svc):
    # 暂停：downloading -> paused
    r = asyncio.run(svc.pause_download(1))
    assert r["status"] == "paused"
    assert svc._paused_phase.get(1) == "downloading"
    # 重复暂停被拒
    with pytest.raises(ValueError, match="无法暂停"):
        asyncio.run(svc.pause_download(1))
    # 继续：paused -> downloading，且启动新下载线程
    r = asyncio.run(svc.resume_download(1))
    assert r["status"] == "downloading"
    assert svc.started == [(1, "test/repo", "main")]
    # 取消：downloading -> cancelled
    r = asyncio.run(svc.cancel_download(1))
    assert r["status"] == "cancelled"
    assert r["error"] == "用户取消"
    # 终态不可再取消/暂停
    with pytest.raises(ValueError, match="无法取消"):
        asyncio.run(svc.cancel_download(1))


def test_cancel_from_paused(svc):
    asyncio.run(svc.pause_download(1))
    r = asyncio.run(svc.cancel_download(1))
    assert r["status"] == "cancelled"
    assert svc._paused_phase.get(1) is None


def test_resume_non_paused_rejected(svc):
    with pytest.raises(ValueError, match="无法继续"):
        asyncio.run(svc.resume_download(1))
