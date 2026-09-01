"""模型任务回归：分发进行中不再误标「下载中」+ 重试就地复用原任务。

- _local_model_status：sending/syncing 阶段直接呈现为对应阶段（而非笼统
  downloading），完整/缺失缓存都不再在分发期间显示为「下载中」；
- retry_download_job：失败任务就地复活（同一 job_id），不再新建第二条记录，
  避免 UI 上「旧失败 + 新下载」并存被误读为再次失败。
"""

import asyncio
import json
import os
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ModelDownload
from app.routers import models as router_models
from app.services import model_manager


def _make_complete_cache(root, repo):
    """按 huggingface_hub 布局构造一个通过 _verify_local_model 的完整假缓存。"""
    from app.services import model_manager as mm

    d = mm.local_model_dir(repo)
    blobs = d / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    blob = blobs / ("a" * 40)
    blob.write_bytes(b"x" * 10)
    sha = "1" * 40
    (d / "trees").mkdir(parents=True, exist_ok=True)
    (d / "trees" / f"{sha}.json").write_text(json.dumps(
        {"model.safetensors": {"size": 10, "blob_id": "a" * 40}}))
    snap = d / "snapshots" / sha
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "model.safetensors").symlink_to(os.path.relpath(blob, snap))
    (d / "refs").mkdir(exist_ok=True)
    (d / "refs" / "main").write_text(sha)


@pytest.fixture()
def cache_root(monkeypatch, tmp_path):
    root = tmp_path / "models-cache"
    monkeypatch.setattr(router_models.config, "MODEL_CACHE_DIR", str(root))
    monkeypatch.setattr(model_manager.config, "MODEL_CACHE_DIR", str(root))
    return root


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    sess = S()
    yield sess
    sess.close()


def _insert_job(db, repo, status, **kw):
    job = ModelDownload(repo=repo, revision=kw.pop("revision", "main"),
                        status=status, **kw)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


# ---------- 本地缓存状态：分发阶段不再误标「下载中」 ----------


def test_local_status_during_distribution_is_phase_not_downloading(db, cache_root):
    _make_complete_cache(cache_root, "org/Model")
    _insert_job(db, "org/Model", "sending", head_node_id=1, total_bytes=10)
    assert router_models._local_model_status(db, "org/Model") == "sending"
    db.query(ModelDownload).delete()
    _insert_job(db, "org/Model", "syncing", head_node_id=1, total_bytes=10)
    assert router_models._local_model_status(db, "org/Model") == "syncing"


def test_local_status_distribution_with_broken_cache_is_phase(db, cache_root):
    """分发任务 + 完整性校验失败的缓存：此前会落到 downloading（下载中）。"""
    _insert_job(db, "org/Model", "sending", head_node_id=1, total_bytes=10)
    assert router_models._local_model_status(db, "org/Model") == "sending"
    _insert_job(db, "other/Model", "syncing", head_node_id=1, total_bytes=10)
    assert router_models._local_model_status(db, "other/Model") == "syncing"


def test_local_status_downloading_phase_and_paused(db, cache_root):
    _insert_job(db, "org/Model", "downloading", total_bytes=10)
    assert router_models._local_model_status(db, "org/Model") == "downloading"
    # 下载完成后：校验通过 -> complete（不再被进行中任务覆盖为 downloading）
    _make_complete_cache(cache_root, "org/Model")
    assert router_models._local_model_status(db, "org/Model") == "complete"
    # 暂停与下载一致：未校验 -> downloading；已校验 -> complete
    db.query(ModelDownload).delete()
    _insert_job(db, "org/Other", "paused", total_bytes=10)
    assert router_models._local_model_status(db, "org/Other") == "downloading"
    _make_complete_cache(cache_root, "org/Other")
    assert router_models._local_model_status(db, "org/Other") == "complete"


def test_local_status_failed_and_partial(db, cache_root):
    _insert_job(db, "org/Model", "failed", total_bytes=10)
    assert router_models._local_model_status(db, "org/Model") == "failed"
    db.query(ModelDownload).delete()
    assert router_models._local_model_status(db, "org/Model") == "partial"
    _make_complete_cache(cache_root, "org/Model")
    assert router_models._local_model_status(db, "org/Model") == "complete"


# ---------- 重试：就地复用原任务，不新建记录 ----------


@pytest.fixture()
def svc(monkeypatch, db, tmp_path):
    """隔离的 service 环境：独立 DB + 桩下载线程/监控（不触网、不开真线程）。"""
    started: list[tuple[int, str, str]] = []
    monitored: list[int] = []

    def fake_start(job_id, repo, revision):
        started.append((job_id, repo, revision))

    async def fake_monitor(job_id):
        monitored.append(job_id)

    monkeypatch.setattr(model_manager, "SessionLocal", lambda: db)
    monkeypatch.setattr(model_manager, "_start_local_download", fake_start)
    monkeypatch.setattr(model_manager, "_monitor_job", fake_monitor)
    monkeypatch.setattr(model_manager, "_download_threads", {})
    monkeypatch.setattr(model_manager, "_download_cancel", {})
    monkeypatch.setattr(model_manager, "_paused_phase", {})
    monkeypatch.setattr(model_manager.config, "MODEL_CACHE_DIR", str(tmp_path / "cache"))
    svc = model_manager
    svc.started = started
    svc.monitored = monitored
    return svc


def test_retry_reuses_same_job_in_place(svc, db):
    job = _insert_job(db, "org/Model", "failed", head_node_id=1,
                      sync_jobs={"2": {"status": "pending"}})
    old_id = job.id
    r = asyncio.run(svc.retry_download_job(old_id))
    assert r.id == old_id  # 同一 job_id，不新建记录
    assert r.status == "downloading"
    assert r.error is None
    # 任务总数不变（只有这一条）
    rows = db.query(ModelDownload).filter(ModelDownload.repo == "org/Model").all()
    assert len(rows) == 1 and rows[0].id == old_id
    # 重启了下载线程并恢复监控
    assert svc.started == [(old_id, "org/Model", "main")]
    assert svc.monitored == [old_id]


def test_retry_rejects_non_failed(svc, db):
    job = _insert_job(db, "org/Model", "completed")
    with pytest.raises(ValueError, match="无法重试"):
        asyncio.run(svc.retry_download_job(job.id))
    assert db.get(ModelDownload, job.id).status == "completed"


def test_retry_rejects_when_other_active_job_exists(svc, db):
    failed = _insert_job(db, "org/Model", "failed")
    _insert_job(db, "org/Model", "downloading", total_bytes=10)
    with pytest.raises(ValueError, match="进行中的传输任务"):
        asyncio.run(svc.retry_download_job(failed.id))
    assert db.get(ModelDownload, failed.id).status == "failed"


def test_retry_waits_for_stale_download_thread(svc, db):
    job = _insert_job(db, "org/Model", "failed")
    class _Alive:
        def __init__(self):
            self.joined = False
            self.cancelled_event = threading.Event()

        def is_alive(self):
            return not self.joined

        def join(self, timeout=None):
            self.joined = True

    t = _Alive()
    svc._download_threads[job.id] = t
    svc._download_cancel[job.id] = t.cancelled_event
    r = asyncio.run(svc.retry_download_job(job.id))
    assert r.id == job.id and r.status == "downloading"
    assert t.joined  # 旧线程被等待退出后才重启，避免双线程写同一批 .part
    assert t.cancelled_event.is_set()
    assert svc.started == [(job.id, "org/Model", "main")]


# ---------- 已下载模型分发不再破坏完整缓存 / 提供补全入口 ----------


def test_local_cache_ready_requires_revision_match(monkeypatch, cache_root):
    """_local_cache_ready：完整缓存 + refs/{revision} 锚点齐备才视为就绪。"""
    _make_complete_cache(cache_root, "org/Model")  # refs/main + trees/<sha>
    assert model_manager._local_cache_ready("org/Model", "main") is True
    # None 归一化为 main
    assert model_manager._local_cache_ready("org/Model", None) is True
    # 其它 revision 无 refs 记录 -> 仍需重新下载
    assert model_manager._local_cache_ready("org/Model", "feature/x") is False
    # 缺 refs 锚点不视为就绪
    (model_manager.local_model_dir("org/Model") / "refs" / "main").unlink()
    assert model_manager._local_cache_ready("org/Model", "main") is False


def test_download_sync_failure_preserves_complete_layout(monkeypatch, cache_root):
    """_download_sync 中途失败不再清空已有完整布局（修复发布分发误破坏缓存）。"""
    _make_complete_cache(cache_root, "org/Model")

    def manifest(*a, **k):
        return {"sha": "2" * 40, "siblings": [
            {"rfilename": "model.safetensors", "size": 10, "blobId": "a" * 40},
            {"rfilename": "new.bin", "size": 5, "blobId": "b" * 40},
        ]}

    def boom(*a, **k):
        raise RuntimeError("网络中断")

    monkeypatch.setattr(model_manager, "_fetch_repo_manifest", manifest)
    monkeypatch.setattr(model_manager, "_download_file_chunked", boom)
    monkeypatch.setattr(model_manager, "get_hf_settings",
                        lambda: {"endpoint": "https://huggingface.co",
                                 "connections": 4, "chunk_size_mb": 8})
    monkeypatch.setattr(model_manager, "_stored_token", lambda: None)

    with pytest.raises(RuntimeError, match="网络中断"):
        model_manager._download_sync("org/Model", "main", None)
    # 旧布局必须原样保留：仍是可校验的完整缓存
    assert model_manager._verify_local_model("org/Model")["ok"]
    d = model_manager.local_model_dir("org/Model")
    assert (d / "refs" / "main").is_file()
    assert len(list((d / "trees").glob("*.json"))) == 1


def test_download_sync_keeps_previous_versions(monkeypatch, cache_root):
    """新版本下载后旧 commit 快照保留（git 式多版本共存），激活版本指向新 sha。"""
    _make_complete_cache(cache_root, "org/Model")  # refs/main -> '1'*40（完整）
    new_sha = "2" * 40
    monkeypatch.setattr(model_manager, "_fetch_repo_manifest", lambda *a, **k: {
        "sha": new_sha,
        "siblings": [{"rfilename": "model.safetensors", "size": 10, "blobId": "a" * 40}],
    })
    monkeypatch.setattr(model_manager, "get_hf_settings",
                        lambda: {"endpoint": "https://huggingface.co",
                                 "connections": 4, "chunk_size_mb": 8})
    monkeypatch.setattr(model_manager, "_stored_token", lambda: None)

    downloaded_sha = model_manager._download_sync("org/Model", "main", None)
    assert downloaded_sha == new_sha  # 下载线程据此回填 task.sha
    assert model_manager._verify_snapshot("org/Model", new_sha)["ok"]
    d = model_manager.local_model_dir("org/Model")
    assert (d / "refs" / "main").read_text().strip() == new_sha
    # 旧 commit 快照与元数据保留：两个版本共存，可零成本回滚/切换
    assert (d / "snapshots" / ("1" * 40)).is_dir()
    assert (d / "trees" / f"{'1' * 40}.json").is_file()
    versions = model_manager._snapshot_versions("org/Model")
    assert {v["sha"] for v in versions} == {new_sha, "1" * 40}
    # 激活版本 = refs/main 指向的新 commit
    assert model_manager._active_snapshot("org/Model") == ("main", new_sha)
    assert model_manager._verify_local_model("org/Model")["ok"]


def test_verify_local_model_uses_active_sha(monkeypatch, cache_root):
    """_verify_local_model 以激活版本为准：历史完整快照不再掩盖当前版本缺失。"""
    _make_complete_cache(cache_root, "org/Model")  # refs/main -> '1'*40（完整）
    d = model_manager.local_model_dir("org/Model")
    # 新 commit：只有 trees 元数据、快照文件缺失（下载中/损坏）——不完整
    broken_sha = "3" * 40
    (d / "trees" / f"{broken_sha}.json").write_text(json.dumps(
        {"model.safetensors": {"size": 10, "blob_id": "a" * 40}}))
    # 激活仍是旧 commit：模型完整
    assert model_manager._verify_local_model("org/Model")["ok"]
    # 把激活指针切到不完整的新 commit：模型必须判定为不完整
    (d / "refs" / "main").write_text(broken_sha)
    assert not model_manager._verify_local_model("org/Model")["ok"]
    assert model_manager._local_cache_ready("org/Model", "main") is False
    versions = model_manager._snapshot_versions("org/Model")
    by_sha = {v["sha"]: v for v in versions}
    assert by_sha[broken_sha]["complete"] is False
    assert by_sha["1" * 40]["complete"] is True


def test_start_download_job_skips_thread_when_cache_ready(monkeypatch, db):
    """目标 revision 缓存已完整：start_download_job 不再启动破坏性下载线程。"""
    started: list[int] = []
    monkeypatch.setattr(model_manager, "SessionLocal", lambda: db)
    monkeypatch.setattr(model_manager, "_start_local_download",
                        lambda j, repo, rev: started.append(j))

    async def fake_monitor(job_id):
        pass

    async def fake_total(repo, revision="main"):
        return 10

    monkeypatch.setattr(model_manager, "_monitor_job", fake_monitor)
    monkeypatch.setattr(model_manager, "_local_cache_ready", lambda repo, rev: True)
    monkeypatch.setattr(model_manager, "repo_total_size", fake_total)

    job = asyncio.run(model_manager.start_download_job("org/Model", "main", None, [], "downloading"))
    assert started == []  # 不启动下载线程
    assert job.status == "downloading"
    assert db.get(ModelDownload, job.id) is not None


def test_start_download_job_starts_thread_when_cache_missing(monkeypatch, db, tmp_path):
    """缓存不完整：仍启动下载线程续传/补齐。"""
    started: list[int] = []
    monkeypatch.setattr(model_manager, "SessionLocal", lambda: db)
    monkeypatch.setattr(model_manager, "_start_local_download",
                        lambda j, repo, rev: started.append(j))

    async def fake_monitor(job_id):
        pass

    async def fake_total(repo, revision="main"):
        return 10

    monkeypatch.setattr(model_manager, "_monitor_job", fake_monitor)
    monkeypatch.setattr(model_manager, "_local_cache_ready", lambda repo, rev: False)
    monkeypatch.setattr(model_manager, "repo_total_size", fake_total)
    monkeypatch.setattr(model_manager.config, "MODEL_CACHE_DIR", str(tmp_path / "cache"))

    job = asyncio.run(model_manager.start_download_job("org/Model", "main", None, [], "downloading"))
    assert started == [job.id]


def test_retry_skips_download_when_cache_ready(svc, db):
    """发送/同步阶段失败的重试：本地完整缓存就绪时不再重跑下载，直接继续分发。"""
    _make_complete_cache(svc.config.MODEL_CACHE_DIR, "org/Model")
    job = _insert_job(db, "org/Model", "failed", head_node_id=1)
    r = asyncio.run(svc.retry_download_job(job.id))
    assert r.id == job.id and r.status == "downloading"
    assert svc.started == []  # 缓存完整 -> 不重跑下载
    assert svc.monitored == [job.id]


def test_start_download_job_records_sha_from_refs(monkeypatch, db, tmp_path):
    """建任务时直接从 refs/<revision> 落库 commit sha（缓存就绪场景，不触网）。"""
    started: list[int] = []
    monkeypatch.setattr(model_manager, "SessionLocal", lambda: db)
    monkeypatch.setattr(model_manager, "_start_local_download",
                        lambda j, repo, rev: started.append(j))

    async def fake_monitor(job_id):
        pass

    async def fake_total(repo, revision="main"):
        return 10

    async def fake_resolve(repo, revision):
        raise AssertionError("缓存就绪时不应触网解析")

    monkeypatch.setattr(model_manager, "_monitor_job", fake_monitor)
    monkeypatch.setattr(model_manager, "repo_total_size", fake_total)
    monkeypatch.setattr(model_manager, "_local_cache_ready", lambda repo, rev: True)
    monkeypatch.setattr(model_manager, "_ref_sha", lambda repo, rev: "deadbeef" * 5)
    monkeypatch.setattr(model_manager, "resolve_revision_sha", fake_resolve)

    job = asyncio.run(model_manager.start_download_job("org/Model", "main", None, [], "downloading"))
    assert job.sha == "deadbeef" * 5
    assert started == []


def test_start_download_job_records_sha_from_resolve(monkeypatch, db, tmp_path):
    """缓存未就绪时尽力从远端解析 sha 并落库（失败返回不阻断流程）。"""
    started: list[int] = []
    monkeypatch.setattr(model_manager, "SessionLocal", lambda: db)
    monkeypatch.setattr(model_manager, "_start_local_download",
                        lambda j, repo, rev: started.append(j))

    async def fake_monitor(job_id):
        pass

    async def fake_total(repo, revision="main"):
        return 10

    async def fake_resolve(repo, revision):
        return "c0ffee" * 5

    monkeypatch.setattr(model_manager, "_monitor_job", fake_monitor)
    monkeypatch.setattr(model_manager, "repo_total_size", fake_total)
    monkeypatch.setattr(model_manager, "_local_cache_ready", lambda repo, rev: False)
    monkeypatch.setattr(model_manager, "_ref_sha", lambda repo, rev: None)
    monkeypatch.setattr(model_manager, "resolve_revision_sha", fake_resolve)
    monkeypatch.setattr(model_manager.config, "MODEL_CACHE_DIR", str(tmp_path / "cache"))

    job = asyncio.run(model_manager.start_download_job("org/Model", "main", None, [], "downloading"))
    assert job.sha == "c0ffee" * 5
    assert started == [job.id]


def test_job_to_dict_includes_sha(db):
    job = _insert_job(db, "org/Model", "completed", sha="abc123" * 5, total_bytes=10)
    d = model_manager.job_to_dict(job)
    assert d["sha"] == "abc123" * 5
    assert d["revision"] == "main"


def test_cached_model_sha_passthrough_and_validation(monkeypatch, tmp_path):
    """/models/cached sha 参数：传给 agent 精确校验；非法 sha 直接 422。"""
    from app.models import Node
    from app.routers.models import cached_model

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(Node(id=1, name="n1", ip="192.0.2.1"))
    db.commit()

    seen = []

    async def fake_cache_repo(node, repo, sha=None):
        seen.append((node.id, repo, sha))
        return {"repo": repo, "complete": True, "verify_sha": sha}

    monkeypatch.setattr("app.services.agent_client.model_cache_repo", fake_cache_repo)

    try:
        # 精确校验：sha 原样传 agent
        r = asyncio.run(cached_model("deepseek-ai/DeepSeek-V4-Flash-0731", 1, "deadbeef", db))
        assert r["complete"] is True and r["verify_sha"] == "deadbeef"
        assert seen == [(1, "deepseek-ai/DeepSeek-V4-Flash-0731", "deadbeef")]
    finally:
        db.close()

    db = S()
    try:
        # 非法 sha（非 hex / 超长）-> 422，不触达 agent
        with pytest.raises(Exception) as exc:
            asyncio.run(cached_model("deepseek-ai/DeepSeek-V4-Flash-0731", 1, "zzz!zzz", db))
        assert getattr(exc.value, "status_code", None) == 422
        assert seen == [(1, "deepseek-ai/DeepSeek-V4-Flash-0731", "deadbeef")]  # 未被调用
    finally:
        db.close()


# ---------- 多版本：按 sha 发送清单 / 清理 GC / worker 差量 / 节点版本校验 ----------


def _add_complete_version(root, repo, sha, blob_id, rel="model.safetensors",
                          size=10, ref_name=None, tree_mtime=None):
    """在既有缓存目录上追加一个完整版本（blobs 内容寻址，可复用已有 blob 文件）。"""
    from app.services import model_manager as mm

    d = mm.local_model_dir(repo)
    blobs = d / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    blob = blobs / blob_id
    if not blob.exists():
        blob.write_bytes(b"y" * size)
    (d / "trees").mkdir(parents=True, exist_ok=True)
    tree = d / "trees" / f"{sha}.json"
    tree.write_text(json.dumps({rel: {"size": size, "blob_id": blob_id}}))
    if tree_mtime is not None:
        os.utime(tree, (tree_mtime, tree_mtime))
    snap = d / "snapshots" / sha
    snap.mkdir(parents=True, exist_ok=True)
    (snap / rel).symlink_to(os.path.relpath(blob, snap))
    if ref_name:
        (d / "refs").mkdir(exist_ok=True)
        (d / "refs" / ref_name).write_text(sha)


def test_snapshot_send_manifest_scopes_to_target_sha(cache_root):
    """按版本分发的最小清单：只含目标快照条目 + 引用 blobs + trees + refs 锚点，
    历史/更新版本的文件一律不下发。"""
    from app.services import model_manager as mm

    _make_complete_cache(cache_root, "org/Versioned")  # sha=1*40, refs/main
    _add_complete_version(cache_root, "org/Versioned", "2" * 40, "2" * 40,
                          rel="tokenizer.json", tree_mtime=2000)
    os.utime(mm.local_model_dir("org/Versioned") / "trees" / ("1" * 40 + ".json"),
             (1000, 1000))

    files = mm._snapshot_send_manifest("org/Versioned", "1" * 40)
    rels = {f["rel"] for f in files}
    assert f"snapshots/{'1'*40}/model.safetensors" in rels
    assert f"blobs/{'a'*40}" in rels
    assert f"trees/{'1'*40}.json" in rels
    assert "refs/main" in rels  # main 指向目标 sha -> 下发锚点
    # 目标 sha 之外（历史/更新版本）不进入发送清单
    assert not any(r.startswith(f"snapshots/{'2'*40}") for r in rels)
    assert f"trees/{'2'*40}.json" not in rels
    assert f"blobs/{'2'*40}" not in rels


def test_prune_repo_versions_protects_refs_and_keep(cache_root, monkeypatch):
    """清理历史版本：refs 目标与最新 keep 个完整版本受保护，其余快照/元数据删除。"""
    from app.services import model_manager as mm

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    monkeypatch.setattr(mm, "SessionLocal", S)

    _make_complete_cache(cache_root, "org/Versioned")      # sha=1*40, refs/main
    _add_complete_version(cache_root, "org/Versioned", "2" * 40, "2" * 40)
    _add_complete_version(cache_root, "org/Versioned", "3" * 40, "3" * 40)
    d = mm.local_model_dir("org/Versioned")
    # 显式控制 trees mtime：确定「最新在前」排序 = [3, 2, 1]
    base = time.time()
    for sha, m in [("1" * 40, base - 100), ("2" * 40, base - 50), ("3" * 40, base)]:
        os.utime(d / "trees" / f"{sha}.json", (m, m))

    # keep=1：refs 保护 1*40，最新 1 个完整（3*40）额外保留 -> 只删 2*40
    deleted = mm.prune_repo_versions("org/Versioned", keep=1)
    assert deleted == ["2" * 40], deleted
    assert not (d / "snapshots" / ("2" * 40)).exists()
    assert not (d / "trees" / ("2" * 40 + ".json")).exists()
    assert (d / "snapshots" / ("1" * 40)).exists()
    assert (d / "snapshots" / ("3" * 40)).exists()
    assert (d / "refs" / "main").read_text().strip() == "1" * 40

    # keep=0：refs 目标仍保护；3*40 不再保留 -> 继续清理
    deleted = mm.prune_repo_versions("org/Versioned", keep=0)
    assert deleted == ["3" * 40], deleted


def test_worker_delta_manifest_maps_missing_to_head_subset(cache_root):
    """worker 差量清单：缺失逻辑文件 -> snapshots/<sha>/<rel> symlink + blobs + trees
    + refs 锚点；旧 Agent（missing_rels=None）退化全量。"""
    from app.services import model_manager as mm

    _make_complete_cache(cache_root, "org/Versioned")  # sha=1*40, blob a*40
    sha = "1" * 40
    share_manifest = [
        {"relpath": f"snapshots/{sha}/model.safetensors", "type": "symlink", "size": 0},
        {"relpath": f"blobs/{'a'*40}", "type": "file", "size": 10},
        {"relpath": f"trees/{sha}.json", "type": "file", "size": 200},
        {"relpath": "refs/main", "type": "file", "size": 40},
        {"relpath": "blobs/unrelated", "type": "file", "size": 999},
    ]

    delta, total = mm._worker_delta_manifest(share_manifest, "org/Versioned", sha, None)
    assert delta is None and total == 0  # 旧 Agent：全量回退

    delta, total = mm._worker_delta_manifest(
        share_manifest, "org/Versioned", sha, ["model.safetensors"])
    rels = {e["relpath"] for e in delta}
    assert rels == {
        f"snapshots/{sha}/model.safetensors",
        f"blobs/{'a'*40}", f"trees/{sha}.json", "refs/main",
    }, rels
    assert "blobs/unrelated" not in rels
    assert total == 10 + 200 + 40


def test_node_model_state_old_agent_returns_none(monkeypatch):
    """节点版本校验：新 Agent 返回 verify_sha 才认定可判定；旧 Agent / 异常 -> None。"""
    from app.models import Node
    from app.services import agent_client
    from app.services.model_manager import _node_model_state

    sha = "1" * 40
    node = Node(id=1, name="n1", ip="10.0.0.1")

    async def old_agent(_node, _repo, sha=None):
        return {"repo": "org/M", "complete": True}  # 无 verify_sha

    async def new_agent(_node, _repo, sha=None):
        return {"repo": "org/M", "complete": True,
                "verify_sha": sha, "missing": []}

    async def crash(_node, _repo, sha=None):
        raise RuntimeError("agent 不可达")

    monkeypatch.setattr(agent_client, "model_cache_repo", old_agent)
    assert asyncio.run(_node_model_state(node, "org/M", sha)) is None
    monkeypatch.setattr(agent_client, "model_cache_repo", new_agent)
    st = asyncio.run(_node_model_state(node, "org/M", sha))
    assert st["verify_sha"] == sha and st["missing"] == []
    monkeypatch.setattr(agent_client, "model_cache_repo", crash)
    assert asyncio.run(_node_model_state(node, "org/M", sha)) is None
