"""模型平铺镜像（models-files/）：硬链接平铺活动版本真实文件，供直接拷贝。

- materialize_plain_files：把激活版本 snapshots/<sha> 平铺到 MODEL_FILES_DIR/<repo-safe>，
  同文件系统硬链接、跨文件系统回退复制，幂等；
- 版本切换后清理镜像中的残留文件；
- os.link 抛 EXDEV（跨盘）时回退 copyfile。
"""

import errno
import hashlib
import json
import os

import pytest

from app.services import model_manager


def _make_complete_cache(root, repo, files):
    """构造通过 _active_snapshot 的完整假 hub 缓存。

    files: {rel: bytes}。单 commit，refs/main 指向该 commit。
    """
    d = model_manager.local_model_dir(repo)
    blobs = d / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    sha = "1" * 40
    tree = {}
    for rel, content in files.items():
        digest = hashlib.sha256(content).hexdigest()
        blob = blobs / digest
        blob.write_bytes(content)
        tree[rel] = {"size": len(content), "blob_id": digest}
    (d / "trees").mkdir(parents=True, exist_ok=True)
    (d / "trees" / f"{sha}.json").write_text(json.dumps(tree))
    snap = d / "snapshots" / sha
    for rel in files:
        link = snap / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(files[rel]).hexdigest()
        link.symlink_to(os.path.relpath(blobs / digest, link.parent))
    (d / "refs").mkdir(exist_ok=True)
    (d / "refs" / "main").write_text(sha)
    return sha


@pytest.fixture()
def cache_root(monkeypatch, tmp_path):
    root = tmp_path / "models-cache"
    files_root = tmp_path / "models-files"
    monkeypatch.setattr(model_manager.config, "MODEL_CACHE_DIR", str(root))
    monkeypatch.setattr(model_manager.config, "MODEL_FILES_DIR", str(files_root))
    return root, files_root


def test_materialize_hardlinks_active_snapshot(cache_root):
    """平铺镜像生成真实文件，同 inode 硬链接，内容一致。"""
    root, files_root = cache_root
    content = {b'{"key": "value"}': "config.json", b"x" * 10: "weights/model.safetensors"}
    _make_complete_cache(root, "org/Model", {
        "config.json": b'{"key": "value"}',
        "weights/model.safetensors": b"x" * 10,
    })
    result = model_manager.materialize_plain_files("org/Model")
    assert result["ok"] and result["strategy"] == "hardlink"
    assert result["files"] == 2 and result["bytes"] == 16 + 10

    mirror = files_root / "org--Model"
    cfg = mirror / "config.json"
    weights = mirror / "weights" / "model.safetensors"
    assert cfg.is_file() and not cfg.is_symlink()
    assert weights.is_file() and not weights.is_symlink()
    assert cfg.read_bytes() == b'{"key": "value"}'
    assert weights.read_bytes() == b"x" * 10
    # 硬链接：与 hub 对应 blob 同一 inode
    for blob_data, rel in content.items():
        hub_blob = root / "models--org--Model" / "blobs" / hashlib.sha256(
            blob_data).hexdigest()
        assert os.stat(mirror / rel).st_ino == os.stat(hub_blob).st_ino


def test_materialize_idempotent_and_cleans_stale(cache_root):
    """重复执行幂等；版本切换（refs 指向新 commit）后清理旧残留文件。"""
    root, files_root = cache_root
    _make_complete_cache(root, "org/Model", {
        "model.safetensors": b"a" * 10,
        "tokenizer.json": b"tok",
    })
    first = model_manager.materialize_plain_files("org/Model")
    assert first["ok"]
    ino = os.stat(files_root / "org--Model" / "model.safetensors").st_ino

    # 同一版本重复执行：不重建、结果一致
    again = model_manager.materialize_plain_files("org/Model")
    assert again["ok"]
    assert os.stat(files_root / "org--Model" / "model.safetensors").st_ino == ino

    # 切换到只含 model.safetensors 的新 commit：tokenizer.json 应被清理
    d = root / "models--org--Model"
    sha2 = "2" * 40
    blob = d / "blobs" / ("b" * 40)
    blob.write_bytes(b"a" * 10)
    (d / "trees" / f"{sha2}.json").write_text(json.dumps(
        {"model.safetensors": {"size": 10, "blob_id": "b" * 40}}))
    snap2 = d / "snapshots" / sha2
    snap2.mkdir(parents=True)
    (snap2 / "model.safetensors").symlink_to(os.path.relpath(blob, snap2))
    (d / "refs" / "main").write_text(sha2)

    changed = model_manager.materialize_plain_files("org/Model")
    assert changed["ok"] and changed["snapshot"] == sha2
    assert (files_root / "org--Model" / "model.safetensors").is_file()
    assert not (files_root / "org--Model" / "tokenizer.json").exists()


def test_materialize_cross_device_falls_back_to_copy(cache_root, monkeypatch):
    """os.link 抛 EXDEV（镜像目录在另一块盘）时回退为复制，不丢文件。"""
    root, files_root = cache_root
    _make_complete_cache(root, "org/Model", {"model.safetensors": b"w" * 8})

    def _exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(model_manager.os, "link", _exdev)
    result = model_manager.materialize_plain_files("org/Model")
    assert result["ok"] and result["strategy"] == "copy"
    cfg = files_root / "org--Model" / "model.safetensors"
    assert cfg.is_file() and not cfg.is_symlink()
    assert cfg.read_bytes() == b"w" * 8
