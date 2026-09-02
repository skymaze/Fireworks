"""跨种类并发下载互斥（模型 <-> 镜像）回归：

控制平面同一时间只允许一个外部下载源——模型 HF 拉取与镜像 registry 拉取互斥；
已完整缓存的资源只做分发（可与对方并发），不受此限制。
"""

import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ImageTransfer, ModelDownload
from app.services import image_manager, model_manager


def _make_complete_model(root: str, repo: str) -> None:
    """构造一个通过 _model_job_target_ready 的完整假缓存（sha = '1'*40）。"""
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


def _touch_image_archive(image: str, size: int = 10) -> None:
    """在 IMAGE_CACHE_DIR 落一个非空归档文件（视为已缓存）。"""
    dest = image_manager.image_archive_path(image)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x" * size)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


@pytest.fixture()
def model_cache(monkeypatch, tmp_path):
    root = tmp_path / "models-cache"
    monkeypatch.setattr(model_manager.config, "MODEL_CACHE_DIR", str(root))
    return root


@pytest.fixture()
def image_cache(monkeypatch, tmp_path):
    root = tmp_path / "images-cache"
    monkeypatch.setattr(image_manager, "IMAGE_CACHE_DIR", root)
    return root


# ---------- 模型下载禁止与镜像拉取并发 ----------


def test_model_download_rejected_while_image_pulling(db, monkeypatch, model_cache, image_cache):
    monkeypatch.setattr(model_manager, "SessionLocal", lambda: db)
    db.add(ImageTransfer(id=1, image="example/img:1", status="pulling"))
    db.commit()
    with pytest.raises(ValueError, match="镜像 example/img:1 正在下载"):
        model_manager._reject_if_image_pulling()


def test_model_download_allowed_when_image_archived(db, monkeypatch, image_cache):
    """镜像归档已落盘（只分发不拉取）：模型下载不被拒绝。"""
    monkeypatch.setattr(model_manager, "SessionLocal", lambda: db)
    _touch_image_archive("example/img:1")
    db.add(ImageTransfer(id=1, image="example/img:1", status="pulling"))
    db.commit()
    model_manager._reject_if_image_pulling()  # 不应抛异常


# ---------- 镜像拉取禁止与模型下载并发 ----------


def test_image_pull_rejected_while_model_downloading(db, monkeypatch, image_cache):
    monkeypatch.setattr(image_manager, "SessionLocal", lambda: db)
    db.add(ModelDownload(id=1, repo="org/Model", status="downloading"))
    db.commit()
    with pytest.raises(ValueError, match="模型 org/Model 正在下载"):
        image_manager._reject_if_model_downloading()


def test_image_pull_allowed_when_model_cached(db, monkeypatch, model_cache, image_cache):
    """模型目标版本已完整（只分发不下载）：镜像拉取不被拒绝。"""
    monkeypatch.setattr(image_manager, "SessionLocal", lambda: db)
    _make_complete_model(str(model_cache), "org/Model")
    db.add(ModelDownload(id=1, repo="org/Model", revision="main",
                         sha="1" * 40, status="downloading"))
    db.commit()
    image_manager._reject_if_model_downloading()  # 不应抛异常


# ---------- 入口校验：download / transfer 会拒绝并发 ----------


async def _fake_resolve_revision_sha(*a, **k):
    return None


async def _fake_repo_total_size(*a, **k):
    return None


@pytest.mark.anyio
async def test_start_download_job_rejects_while_image_pulling(
    db, monkeypatch, model_cache, image_cache,
):
    monkeypatch.setattr(model_manager, "SessionLocal", lambda: db)
    monkeypatch.setattr(model_manager, "repo_total_size", _fake_repo_total_size)
    monkeypatch.setattr(model_manager, "resolve_revision_sha", _fake_resolve_revision_sha)
    db.add(ImageTransfer(id=1, image="example/img:1", status="pulling"))
    db.commit()
    with pytest.raises(ValueError, match="不能与模型同时下载"):
        await model_manager.start_download_job("org/Model", "main", None, [])


@pytest.mark.anyio
async def test_start_image_transfer_rejects_while_model_downloading(
    db, monkeypatch, image_cache,
):
    monkeypatch.setattr(image_manager, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        image_manager, "inspect_image",
        lambda image: {"digest": "sha256:abc", "size_bytes": 10},
    )
    db.add(ModelDownload(id=1, repo="org/Model", status="downloading",
                         revision="main", sha="2" * 40))
    db.commit()
    with pytest.raises(ValueError, match="不能与镜像同时下载"):
        await image_manager.start_image_transfer("example/img:1", None, [])


@pytest.mark.anyio
async def test_start_download_job_allows_cached_image_distribution(
    db, monkeypatch, model_cache, image_cache,
):
    """镜像已缓存 + 模型缺失：只拒绝「下载下载」重叠，这里模型下载应正常放行。"""
    monkeypatch.setattr(model_manager, "SessionLocal", lambda: db)
    monkeypatch.setattr(model_manager, "repo_total_size", _fake_repo_total_size)
    monkeypatch.setattr(model_manager, "resolve_revision_sha", _fake_resolve_revision_sha)
    # 阻止真实下载线程与监控（校验被拒后不应走到创建任务）
    monkeypatch.setattr(model_manager, "_start_local_download", lambda *a: None)
    monkeypatch.setattr(model_manager, "spawn", lambda *a: None)
    _touch_image_archive("example/img:1")
    db.add(ImageTransfer(id=1, image="example/img:1", status="pulling"))
    db.commit()
    job = await model_manager.start_download_job("org/Model", "main", None, [])
    assert job.repo == "org/Model"  # 未被并发下载互斥拒绝
