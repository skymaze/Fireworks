"""Agent 部署流程单测：token 生成/注入/落库/验证时序 + 容器化部署链路。

覆盖：部署即轮换（每次部署新 token）、成功落库并同步内存对象、
失败不落库（旧 token 保持）、镜像链路（预检/拉取/上传/执行脚本）、
sftp 分块上传与断点续传。
"""

import asyncio
import hashlib
import io
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.db import Base
from app.models import Node
from app.services import deploy_agent, image_manager, ssh_client


@pytest.fixture()
def S():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _add_node(S, **kw) -> int:
    with S() as db:
        node = Node(name="n1", ip="10.0.0.9", ssh_username="spark", ssh_password="x", **kw)
        db.add(node)
        db.commit()
        return node.id


def _deploy(monkeypatch, S, node_id, deploy_result, info_ok=True):
    """跑 deploy() 主流程；返回 (result, 本次注入的 token)。"""
    captured: dict = {}

    def fake_deploy_sync(node, token):
        captured["token"] = token
        return deploy_result

    async def fake_info(node):
        if not info_ok:
            raise RuntimeError("agent unreachable")
        return {"hostname": "n1"}

    monkeypatch.setattr(deploy_agent, "_deploy_sync", fake_deploy_sync)
    monkeypatch.setattr(deploy_agent.agent_client, "info", fake_info)
    monkeypatch.setattr(deploy_agent, "SessionLocal", S)

    async def run():
        with S() as db:
            node = db.get(Node, node_id)
            return await deploy_agent.deploy(node)

    return asyncio.run(run()), captured.get("token")


def _token_in_db(S, node_id) -> str | None:
    with S() as db:
        row = db.get(Node, node_id)
        return row.agent_token


def test_deploy_success_rotates_and_persists_token(monkeypatch, S):
    """部署成功：生成新 token 注入、落库、内存对象同步（info 用新 token 验证）。"""
    node_id = _add_node(S)
    result, token = _deploy(monkeypatch, S, node_id, {"ok": True, "install_dir": "/opt/fireworks-agent"})
    assert result["ok"] is True
    assert token and len(token) >= 40
    # token_urlsafe 字符集天然合规（deploy-container.sh 同款校验）
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert _token_in_db(S, node_id) == token


def test_deploy_failure_keeps_old_token(monkeypatch, S):
    """部署失败：新 token 不落库（旧 token 保持，旧 Agent 继续工作）。"""
    node_id = _add_node(S, agent_token="old-token")
    result, token = _deploy(monkeypatch, S, node_id, {"ok": False, "error": "ssh failed"})
    assert result["ok"] is False
    assert _token_in_db(S, node_id) == "old-token"


def test_deploy_info_failure_still_persists_token(monkeypatch, S):
    """部署成功但连通性验证失败：token 仍落库（Agent 已用新 token 运行，warning 提示）。"""
    node_id = _add_node(S)
    result, token = _deploy(monkeypatch, S, node_id,
                            {"ok": True, "install_dir": "/opt/fireworks-agent"}, info_ok=False)
    assert result["ok"] is True and "warning" in result
    assert _token_in_db(S, node_id) == token


# ---------- 容器化部署链路（_deploy_sync） ----------


def _fake_ssh(monkeypatch, cmds: dict, connect_exc=None):
    """构造 mock ssh：按命令前缀返回 (stdout, stderr, rc)；记录调用。"""
    calls: list[str] = []

    def fake_connect(node, timeout=15):
        if connect_exc:
            raise connect_exc
        return SimpleNamespace(close=lambda: None)

    def fake_exec(client, command, timeout=60):
        calls.append(command)
        for prefix, resp in cmds.items():
            if command.startswith(prefix):
                return resp
        return "", "", 0

    def fake_put(client, local_path, remote_path):
        calls.append(f"sftp:{remote_path}")

    monkeypatch.setattr(ssh_client, "connect", fake_connect)
    monkeypatch.setattr(ssh_client, "exec", fake_exec)
    monkeypatch.setattr(ssh_client, "sftp_put", fake_put)
    return calls


def test_deploy_sync_container_flow(monkeypatch, tmp_path):
    """容器化链路：docker 预检 -> 架构探测 -> 拉取镜像 -> 上传 -> 执行部署脚本。"""
    calls = _fake_ssh(monkeypatch, {
        "docker version": ("27.5.1", "", 0),
        "uname -m": ("aarch64", "", 0),
        "deploy-container.sh": ("ok", "", 0),
    })
    monkeypatch.setattr(image_manager, "IMAGE_CACHE_DIR", tmp_path)
    pulled = []

    def fake_pull(image, dest, arch="arm64"):
        pulled.append((image, str(dest), arch))
        dest.write_bytes(b"tar-bytes")

    monkeypatch.setattr(image_manager, "pull_image", fake_pull)
    node = Node(id=1, name="n1", ip="10.0.0.9", agent_port=9000, ssh_username="root")
    result = deploy_agent._deploy_sync(node, "tok-123")

    assert result["ok"] is True
    assert result["install_dir"] == "/opt/fireworks-agent"
    # 拉镜像按节点架构；缓存按镜像引用哈希命名，第二次部署（同引用）复用不重拉
    assert pulled and pulled[0][2] == "arm64"
    cache_name = "agent-" + hashlib.sha256(b"ghcr.io/skymaze/fireworks-agent:latest").hexdigest()[:8] + "-arm64.tar"
    assert (tmp_path / cache_name).read_bytes() == b"tar-bytes"
    # 上传 tar + 脚本，然后执行部署脚本（带 token/arch/image）
    sftp_targets = [c for c in calls if c.startswith("sftp:")]
    assert any("agent-image.tar" in c for c in sftp_targets)
    assert any("deploy-container.sh" in c for c in sftp_targets)
    deploy_cmd = next(c for c in calls if "deploy-container.sh" in c and not c.startswith("sftp:"))
    assert "FW_AGENT_TOKEN='tok-123'" in deploy_cmd
    assert "arm64" in deploy_cmd
    assert "ghcr.io/skymaze/fireworks-agent:latest" in deploy_cmd
    # 缓存复用：第二次部署不重复拉取
    pulled.clear()
    result2 = deploy_agent._deploy_sync(node, "tok-456")
    assert result2["ok"] is True and pulled == []


def test_deploy_sync_amd64_mapping(monkeypatch, tmp_path):
    """x86_64 节点映射为 amd64 并拉取对应架构镜像。"""
    calls = _fake_ssh(monkeypatch, {
        "docker version": ("27.5.1", "", 0),
        "uname -m": ("x86_64", "", 0),
    })
    monkeypatch.setattr(image_manager, "IMAGE_CACHE_DIR", tmp_path)
    pulled = []
    monkeypatch.setattr(image_manager, "pull_image",
                        lambda image, dest, arch="arm64": pulled.append(arch) or dest.write_bytes(b"t"))
    node = Node(id=1, name="n1", ip="10.0.0.9", agent_port=9000, ssh_username="root")
    result = deploy_agent._deploy_sync(node, "tok-123")
    assert result["ok"] is True
    assert pulled == ["amd64"]
    assert "deploy-container.sh" in next(c for c in calls if "deploy-container.sh" in c)


def test_deploy_sync_cache_keyed_by_image_ref(monkeypatch, tmp_path):
    """镜像引用（换源/换 tag）变化时缓存自动失效并重新拉取。"""
    monkeypatch.setattr(config, "AGENT_IMAGE", "ghcr.io/skymaze/fireworks/agent:v2")
    _fake_ssh(monkeypatch, {
        "docker version": ("27.5.1", "", 0),
        "uname -m": ("aarch64", "", 0),
        "deploy-container.sh": ("ok", "", 0),
    })
    monkeypatch.setattr(image_manager, "IMAGE_CACHE_DIR", tmp_path)
    pulled = []
    monkeypatch.setattr(image_manager, "pull_image",
                        lambda image, dest, arch="arm64": pulled.append(image) or dest.write_bytes(b"t"))
    node = Node(id=1, name="n1", ip="10.0.0.9", agent_port=9000, ssh_username="root")
    result = deploy_agent._deploy_sync(node, "tok-123")
    assert result["ok"] is True
    assert pulled == ["ghcr.io/skymaze/fireworks/agent:v2"]
    assert len(list(tmp_path.iterdir())) == 1  # 仅 v2 缓存


def test_deploy_sync_requires_docker(monkeypatch):
    """节点 docker 不可用：明确报错，不继续部署。"""
    calls = _fake_ssh(monkeypatch, {
        "docker version": ("", "permission denied", 1),
    })
    node = Node(id=1, name="n1", ip="10.0.0.9", agent_port=9000, ssh_username="spark")
    result = deploy_agent._deploy_sync(node, "tok-123")
    assert result["ok"] is False
    assert "docker 不可用" in result["error"]
    assert not any("deploy-container.sh" in c for c in calls)


def test_deploy_sync_rejects_bad_token(monkeypatch, tmp_path):
    """token 含非法字符：拒绝部署（防御命令行注入）。"""
    _fake_ssh(monkeypatch, {
        "docker version": ("27.5.1", "", 0),
        "uname -m": ("aarch64", "", 0),
    })
    monkeypatch.setattr(image_manager, "IMAGE_CACHE_DIR", tmp_path)
    node = Node(id=1, name="n1", ip="10.0.0.9", agent_port=9000, ssh_username="root")
    result = deploy_agent._deploy_sync(node, "tok' ; rm -rf /")
    assert result["ok"] is False
    assert "非法字符" in result["error"]


# ---------- sftp 分块上传 / 断点续传 ----------


class _FakeSFTP:
    """内存版 SFTP：.part 续传语义（stat/open/rename）。"""

    def __init__(self, existing: bytes | None):
        self.data = existing  # None = .part 不存在
        self.renamed = None

    def stat(self, path):
        if path.endswith(".part") and self.data is not None:
            return SimpleNamespace(st_size=len(self.data))
        raise FileNotFoundError(path)

    def rename(self, src, dst):
        assert src.endswith(".part")
        self.renamed = dst

    def open(self, path, mode):
        if "a" in mode:
            buf = io.BytesIO(self.data or b"")
            buf.seek(0, 2)
        else:
            buf = io.BytesIO()
        return _FakeSFTPFile(buf, self)

    def close(self):
        pass


class _FakeSFTPFile:
    def __init__(self, buf, sftp):
        self._buf = buf
        self._sftp = sftp

    def write(self, data):
        self._buf.write(data)
        self._sftp.data = self._buf.getvalue()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _put(local_path, data: bytes, existing: bytes | None = None):
    sftp_obj = _FakeSFTP(existing)
    client = SimpleNamespace(open_sftp=lambda: sftp_obj)
    with open(local_path, "wb") as f:
        f.write(data)
    ssh_client.sftp_put(client, str(local_path), "/remote/agent-image.tar")
    return sftp_obj


def test_sftp_put_full_upload(tmp_path):
    """无 .part：从头分块上传并 rename。"""
    data = b"x" * (10 << 20)  # 多块
    sf = _put(tmp_path / "src.tar", data)
    assert sf.data == data
    assert sf.renamed == "/remote/agent-image.tar"


def test_sftp_put_resumes_from_part(tmp_path):
    """已有 .part：从已有字节数续传，最终内容一致。"""
    data = b"hello world" * 5000
    sf = _put(tmp_path / "src.tar", data, existing=data[:7])
    assert sf.data == data
    assert sf.renamed == "/remote/agent-image.tar"


def test_sftp_put_part_already_complete(tmp_path):
    """.part 已完整：直接 rename，不再传输。"""
    data = b"done"
    sf = _put(tmp_path / "src.tar", data, existing=data)
    assert sf.data == data
    assert sf.renamed == "/remote/agent-image.tar"
