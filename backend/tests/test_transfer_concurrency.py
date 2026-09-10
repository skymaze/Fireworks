"""传输并发准入：只禁止「同一模型/镜像 -> 同一台机器」的重复传输。

允许：
- 模型与镜像同时传输（下载与节点分发均可并发）；
- 多个模型、多个镜像同时传输；
- 同一模型/镜像同时分发到**不相交**的节点集合（如分发到不同集群）。
禁止：
- 同一模型/镜像 -> 机器集合相交的重复分发（同机同文件并发写）；
- 同一模型/镜像的重复真实下载/拉取（同写控制平面缓存/归档）。
"""

import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Node, ModelDownload, ImageTransfer
from app.services import image_manager, model_manager


def _make_complete_model(root: str, repo: str) -> None:
    """构造一个通过 _local_cache_ready 的完整假缓存（refs/main -> sha）。"""
    d = model_manager.local_model_dir(repo)
    blob = d / "blobs" / ("a" * 40)
    blob.parent.mkdir(parents=True, exist_ok=True)
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


def _touch_image_archive(image: str, size: int = 10) -> None:
    dest = image_manager.image_archive_path(image)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x" * size)


async def _fake_repo_total_size(*_a, **_k):
    return None


async def _fake_resolve_revision_sha(*_a, **_k):
    return None


@pytest.fixture()
def db(monkeypatch, tmp_path):
    """文件库 + 4 个节点；所有外部副作用（线程/子进程/远端）都被打桩。"""
    engine = create_engine(f"sqlite:///{tmp_path}/fw.db")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    monkeypatch.setattr(model_manager, "SessionLocal", S)
    monkeypatch.setattr(image_manager, "SessionLocal", S)
    monkeypatch.setattr(model_manager.config, "MODEL_CACHE_DIR", str(tmp_path / "models-cache"))
    monkeypatch.setattr(image_manager, "IMAGE_CACHE_DIR", tmp_path / "images-cache")
    monkeypatch.setattr(model_manager, "repo_total_size", _fake_repo_total_size)
    monkeypatch.setattr(model_manager, "resolve_revision_sha", _fake_resolve_revision_sha)
    monkeypatch.setattr(model_manager, "_start_local_download", lambda *a, **k: None)
    monkeypatch.setattr(image_manager, "_start_pull", lambda *a, **k: None)
    monkeypatch.setattr(model_manager, "spawn", lambda coro: coro.close())
    monkeypatch.setattr(image_manager, "spawn", lambda coro: coro.close())
    session = S()
    session.add_all([Node(id=i, name=f"n{i}", ip=f"192.0.2.{i}") for i in range(1, 5)])
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def image_meta(monkeypatch):
    """inspect 返回空 digest：归档存在即视为已缓存（needs_pull=False）。"""
    monkeypatch.setattr(
        image_manager, "inspect_image",
        lambda _image: {"digest": "", "size_bytes": 10},
    )


# ---------- 模型 x 模型 ----------


@pytest.mark.anyio
async def test_same_model_disjoint_nodes_allowed(db):
    """同一模型分发到不相交节点集合（不同集群）可并发。"""
    _make_complete_model(str(model_manager.config.MODEL_CACHE_DIR), "org/Model")
    first = await model_manager.start_download_job("org/Model", "main", 1, [2])
    second = await model_manager.start_download_job("org/Model", "main", 3, [4])
    assert first.id != second.id
    assert db.query(ModelDownload).count() == 2


@pytest.mark.anyio
async def test_same_model_overlapping_nodes_rejected(db):
    """同一模型 -> 机器集合相交的重复分发被拒绝（含只重叠一台 worker）。"""
    _make_complete_model(str(model_manager.config.MODEL_CACHE_DIR), "org/Model")
    await model_manager.start_download_job("org/Model", "main", 1, [2])
    with pytest.raises(ValueError, match="正在向相同节点分发"):
        await model_manager.start_download_job("org/Model", "main", 2, [3])


@pytest.mark.anyio
async def test_same_model_disjoint_nodes_still_rejected_when_downloading(db):
    """本次需要真实下载时，即使目标节点不相交也拒绝（同写控制平面缓存）。"""
    await model_manager.start_download_job("org/Model", "main", 1, [2])
    with pytest.raises(ValueError, match="本次需要下载版本"):
        await model_manager.start_download_job("org/Model", "main", 3, [4])


@pytest.mark.anyio
async def test_different_models_concurrent(db):
    """多个模型可同时传输（含同时真实下载）。"""
    a = await model_manager.start_download_job("org/ModelA", "main", 1, [2])
    b = await model_manager.start_download_job("org/ModelB", "main", 3, [4])
    assert a.id != b.id
    assert db.query(ModelDownload).count() == 2


# ---------- 镜像 x 镜像 ----------


@pytest.mark.anyio
async def test_same_image_disjoint_nodes_allowed(db, image_meta):
    """同一镜像分发到不相交节点集合可并发（归档已缓存，仅分发）。"""
    _touch_image_archive("example/img:1")
    first = await image_manager.start_image_transfer("example/img:1", 1, [2])
    second = await image_manager.start_image_transfer("example/img:1", 3, [4])
    assert first.id != second.id
    assert db.query(ImageTransfer).count() == 2


@pytest.mark.anyio
async def test_same_image_overlapping_nodes_rejected(db, image_meta):
    """同一镜像 -> 机器集合相交的重复分发被拒绝。"""
    _touch_image_archive("example/img:1")
    await image_manager.start_image_transfer("example/img:1", 1, [2, 3])
    with pytest.raises(ValueError, match="正在向相同节点分发"):
        await image_manager.start_image_transfer("example/img:1", 3, [4])


@pytest.mark.anyio
async def test_same_image_disjoint_nodes_still_rejected_when_pulling(db, image_meta):
    """本次需要真实拉取时，即使目标节点不相交也拒绝（同写控制平面归档）。"""
    await image_manager.start_image_transfer("example/img:1", 1, [2])
    with pytest.raises(ValueError, match="本次需要重新拉取"):
        await image_manager.start_image_transfer("example/img:1", 3, [4])


@pytest.mark.anyio
async def test_different_images_concurrent(db, image_meta):
    """多个镜像可同时拉取/分发。"""
    a = await image_manager.start_image_transfer("example/img:1", 1, [2])
    b = await image_manager.start_image_transfer("example/img:2", 3, [4])
    assert a.id != b.id
    assert db.query(ImageTransfer).count() == 2


# ---------- 模型 <-> 镜像 ----------


@pytest.mark.anyio
async def test_model_download_allowed_while_image_pulling(db):
    """镜像正在拉取时模型仍可下载（不再跨种类互斥）。"""
    db.add(ImageTransfer(id=1, image="example/img:1", status="pulling"))
    db.commit()
    job = await model_manager.start_download_job("org/Model", "main", None, [])
    assert job.repo == "org/Model"


@pytest.mark.anyio
async def test_image_pull_allowed_while_model_downloading(db, image_meta):
    """模型正在下载时镜像仍可拉取（不再跨种类互斥）。"""
    db.add(ModelDownload(id=1, repo="org/Model", revision="main",
                         sha="2" * 40, status="downloading"))
    db.commit()
    t = await image_manager.start_image_transfer("example/img:1", None, [])
    assert t.image == "example/img:1"
