"""Agent 侧平铺镜像：/api/model/materialize 生成 models/ 真实文件，删除时一并清理。"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import main as agent_main  # noqa: E402

AUTH = {"Authorization": "Bearer agent-test-token"}


@pytest.fixture(autouse=True)
def _agent_token(monkeypatch):
    monkeypatch.setattr(agent_main, "AGENT_TOKEN", "agent-test-token")


def _make_hub_cache(tmp_path, repo="owner/repo"):
    """构造节点 hub 缓存：blobs + snapshots symlink + refs/main。"""
    root = tmp_path / "hub" / "models--owner--repo"
    content = b"model-weights"
    digest = hashlib.sha256(content).hexdigest()
    blob = root / "blobs" / digest
    blob.parent.mkdir(parents=True)
    blob.write_bytes(content)
    (root / "refs").mkdir()
    (root / "refs" / "main").write_text("commit")
    snapshot = root / "snapshots" / "commit"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").symlink_to(f"../../blobs/{digest}")
    return root, blob, content, digest


def test_model_materialize_endpoint_creates_plain_mirror(monkeypatch, tmp_path):
    """端点把活动版本平铺到 models/<repo-safe>/，硬链接且可读内容一致。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "DEFAULT_HF_CACHE", tmp_path)
    monkeypatch.setattr(agent_main, "MODEL_FILES_DIR", tmp_path / "models")
    root, blob, content, digest = _make_hub_cache(tmp_path)

    client = TestClient(agent_main.app)
    resp = client.post("/api/model/materialize",
                       json={"repo": "owner/repo"}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] and data["snapshot"] == "commit"
    assert data["strategy"] == "hardlink" and data["files"] == 1

    mirror = tmp_path / "models" / "owner--repo" / "model.bin"
    assert mirror.is_file() and not mirror.is_symlink()
    assert mirror.read_bytes() == content
    assert os.stat(mirror).st_ino == os.stat(blob).st_ino


def test_model_materialize_requires_auth(monkeypatch, tmp_path):
    """未带 token 的请求被拒绝。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "DEFAULT_HF_CACHE", tmp_path)
    monkeypatch.setattr(agent_main, "MODEL_FILES_DIR", tmp_path / "models")
    _make_hub_cache(tmp_path)
    client = TestClient(agent_main.app)
    resp = client.post("/api/model/materialize", json={"repo": "owner/repo"})
    assert resp.status_code == 401


def test_model_delete_removes_mirror(monkeypatch, tmp_path):
    """删除模型时平铺镜像目录一并清除。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "DEFAULT_HF_CACHE", tmp_path)
    monkeypatch.setattr(agent_main, "MODEL_FILES_DIR", tmp_path / "models")
    root, blob, _, _ = _make_hub_cache(tmp_path)
    assert blob.exists()

    client = TestClient(agent_main.app)
    assert client.post("/api/model/materialize", json={
        "repo": "owner/repo"}, headers=AUTH).json()["ok"]
    mirror = tmp_path / "models" / "owner--repo"
    assert mirror.exists()

    deleted = client.delete("/api/model/owner/repo", headers=AUTH)
    assert deleted.status_code == 200 and deleted.json()["deleted"]
    assert not root.exists() and not mirror.exists()


def test_model_materialize_no_snapshot_is_400(monkeypatch, tmp_path):
    """无可用快照（模型未下载）时返回 400 而不是静默成功。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "DEFAULT_HF_CACHE", tmp_path)
    monkeypatch.setattr(agent_main, "MODEL_FILES_DIR", tmp_path / "models")
    client = TestClient(agent_main.app)
    resp = client.post("/api/model/materialize",
                       json={"repo": "missing/repo"}, headers=AUTH)
    assert resp.status_code == 400
