"""Agent 部署流程单测：token 生成/注入/落库/验证时序（无真实节点，mock SSH 与 HTTP）。

覆盖：部署即轮换（每次部署新 token）、成功落库并同步内存对象、
失败不落库（旧 token 保持）、token 字符集天然合规。
"""

import asyncio
import io
from types import SimpleNamespace
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Node
from app.services import deploy_agent, ssh_client


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
    # token_urlsafe 字符集天然合规（deploy.sh 同款校验）
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert _token_in_db(S, node_id) == token


def test_deploy_failure_keeps_old_token(monkeypatch, S):
    """部署失败：新 token 不落库，现有 Agent 继续使用原 token。"""
    node_id = _add_node(S, agent_token="old-token")
    result, token = _deploy(monkeypatch, S, node_id, {"ok": False, "error": "ssh failed"})
    assert result["ok"] is False
    assert _token_in_db(S, node_id) == "old-token"


def test_deploy_sync_exception_returns_failure_not_500(monkeypatch, S):
    """_deploy_sync 抛出异常（如 SSH 连接超时）：deploy 兜底返回 ok=False，
    不把裸异常抛给上层（否则添加节点/重部署会得到 500 而非清晰报错）。"""
    node_id = _add_node(S, agent_token="old-token")

    def fake_deploy_sync(node, token):
        raise TimeoutError("timed out")

    async def fake_info(node):
        raise AssertionError("部署失败不应走到连通性验证")

    monkeypatch.setattr(deploy_agent, "_deploy_sync", fake_deploy_sync)
    monkeypatch.setattr(deploy_agent.agent_client, "info", fake_info)
    monkeypatch.setattr(deploy_agent, "SessionLocal", S)

    async def run():
        with S() as db:
            node = db.get(Node, node_id)
            return await deploy_agent.deploy(node)

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "timed out" in result["error"]
    assert _token_in_db(S, node_id) == "old-token"  # 新 token 未落库


def test_deploy_info_failure_still_persists_token(monkeypatch, S):
    """部署成功但连通性验证失败：token 仍落库（Agent 已用新 token 运行，warning 提示）。"""
    node_id = _add_node(S)
    result, token = _deploy(monkeypatch, S, node_id,
                            {"ok": True, "install_dir": "/opt/fireworks-agent"}, info_ok=False)
    assert result["ok"] is True and "warning" in result
    assert _token_in_db(S, node_id) == token


# ---------- 离线 wheelhouse 部署链路 ----------


def test_deploy_sync_uploads_wheels_and_runs_deploy_sh(monkeypatch, tmp_path):
    """容器化回退后：上传 main.py/requirements/deploy.sh + wheels 目录，执行 deploy.sh。"""
    from app.services import ssh_client as sc

    # 本地 agent 目录（含 wheels/py3.10/...）
    fake_agent = tmp_path / "agent"
    fake_agent.mkdir()
    (fake_agent / "main.py").write_text("x")
    (fake_agent / "requirements.txt").write_text("x")
    (fake_agent / "deploy.sh").write_text("x")
    (fake_agent / "wheels" / "py3.10").mkdir(parents=True)
    (fake_agent / "wheels" / "py3.10" / "fastapi.whl").write_bytes(b"whl")
    (fake_agent / "wheels" / "py3.11").mkdir(parents=True)
    (fake_agent / "wheels" / "py3.11" / "psutil.whl").write_bytes(b"whl")
    monkeypatch.setattr(deploy_agent, "LOCAL_AGENT_DIR", fake_agent)

    calls: list[str] = []

    def fake_connect(node, timeout=15):
        return SimpleNamespace(close=lambda: None)

    def fake_exec(client, command, timeout=60):
        calls.append(command)
        if "bash" in command and "deploy.sh" in command:
            assert "FW_AGENT_TOKEN='tok-123'" in command
        return "", "", 0

    def fake_put(client, local, remote):
        calls.append(f"put:{remote}")

    def fake_put_dir(client, local, remote):
        calls.append(f"putdir:{remote}")

    monkeypatch.setattr(sc, "connect", fake_connect)
    monkeypatch.setattr(sc, "exec", fake_exec)
    monkeypatch.setattr(sc, "sftp_put", fake_put)
    monkeypatch.setattr(sc, "sftp_put_dir", fake_put_dir)
    monkeypatch.setattr(deploy_agent.config, "AGENT_DEPLOY_DIR", "/opt/fireworks-agent")

    node = Node(id=1, name="n1", ip="10.0.0.9", agent_port=9000, ssh_username="root")
    result = deploy_agent._deploy_sync(node, "tok-123")

    assert result["ok"] is True
    # 三个文件 + wheels 目录都上传
    assert any("put:/opt/fireworks-agent/main.py" in c for c in calls)
    assert any("put:/opt/fireworks-agent/requirements.txt" in c for c in calls)
    assert any("put:/opt/fireworks-agent/deploy.sh" in c for c in calls)
    assert any("putdir:/opt/fireworks-agent/wheels" in c for c in calls)
    assert any("deploy.sh" in c and "FW_AGENT_TOKEN='tok-123'" in c for c in calls)


def test_deploy_sync_missing_wheels_fails(monkeypatch, tmp_path):
    """控制平面缺 wheels 目录：明确报错，不开始 SSH 部署。"""
    fake_agent = tmp_path / "agent"
    fake_agent.mkdir()
    (fake_agent / "main.py").write_text("x")
    (fake_agent / "requirements.txt").write_text("x")
    (fake_agent / "deploy.sh").write_text("x")
    monkeypatch.setattr(deploy_agent, "LOCAL_AGENT_DIR", fake_agent)

    connected = []

    def fake_connect(node, timeout=15):
        connected.append(True)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(ssh_client, "connect", fake_connect)
    node = Node(id=1, name="n1", ip="10.0.0.9", agent_port=9000, ssh_username="root")
    result = deploy_agent._deploy_sync(node, "tok-123")
    assert result["ok"] is False
    assert "离线依赖包" in result["error"]
    assert connected == []


# ---------- sftp 递归上传 ----------


def test_sftp_put_dir_recurses(tmp_path, monkeypatch):
    """sftp_put_dir：递归创建远端目录并逐文件上传。"""
    local = tmp_path / "wheels"
    (local / "py3.10").mkdir(parents=True)
    (local / "py3.10" / "a.whl").write_bytes(b"a")
    (local / "py3.11").mkdir(parents=True)
    (local / "py3.11" / "b.whl").write_bytes(b"b")

    mkdirs: list[str] = []
    puts: list[tuple[str, str]] = []

    class _FakeSFTP:
        def stat(self, path):
            raise FileNotFoundError(path)

        def mkdir(self, path):
            mkdirs.append(path)

        def close(self):
            pass

    def fake_put(client, local_path, remote_path):
        puts.append((local_path, remote_path))

    monkeypatch.setattr(ssh_client, "sftp_put", fake_put)
    client = SimpleNamespace(open_sftp=lambda: _FakeSFTP())
    ssh_client.sftp_put_dir(client, str(local), "/remote/wheels")

    assert set(mkdirs) == {"/remote/wheels", "/remote/wheels/py3.10", "/remote/wheels/py3.11"}
    assert any(p[1] == "/remote/wheels/py3.10/a.whl" for p in puts)
    assert any(p[1] == "/remote/wheels/py3.11/b.whl" for p in puts)


# ---------- sftp_put 目标已存在处理 ----------


def test_sftp_put_removes_existing_target(tmp_path, monkeypatch):
    """目标已存在（旧部署残留）：rename 前先删除，避免 sftp-server 拒绝覆盖 rename。"""
    class _FakeSFTP:
        def __init__(self):
            self.part_data = None
            self.removed: list[str] = []
            self.renamed: str | None = None

        def stat(self, path):
            if path.endswith(".part") and self.part_data is not None:
                return SimpleNamespace(st_size=len(self.part_data))
            raise FileNotFoundError(path)

        def remove(self, path):
            self.removed.append(path)

        def rename(self, src, dst):
            assert src.endswith(".part")
            self.renamed = dst

        def open(self, path, mode):
            buf = io.BytesIO(self.part_data or b"")
            if "a" in mode:
                buf.seek(0, 2)
            return _FakeSFTPFile(buf, self)

        def close(self):
            pass

    class _FakeSFTPFile:
        def __init__(self, buf, sftp):
            self._buf = buf
            self._sftp = sftp

        def write(self, data):
            self._buf.write(data)
            self._sftp.part_data = self._buf.getvalue()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    local = tmp_path / "main.py"
    local.write_bytes(b"agent-code")

    sftp = _FakeSFTP()
    client = SimpleNamespace(open_sftp=lambda: sftp)
    ssh_client.sftp_put(client, str(local), "/remote/main.py")

    assert sftp.removed == ["/remote/main.py"]  # 先删旧目标
    assert sftp.renamed == "/remote/main.py"    # 再 rename 收尾
    assert sftp.part_data == b"agent-code"


# ---------- 部署前磁盘空间预检（磁盘写满时 SFTP 只报不透明的 Failure） ----------


def test_remote_free_bytes_parses_df(monkeypatch):
    """解析 df -Pk 的 Available 列（KiB -> 字节）；命令失败/格式异常返回 None。"""
    client = SimpleNamespace()

    def ok_exec(_client, _cmd, **_kw):
        return ("/dev/nvme0n1p2 960000000 900000000 60000000 94% /", "", 0)

    monkeypatch.setattr(ssh_client, "exec", ok_exec)
    assert deploy_agent._remote_free_bytes(client, "/home/spark/.fireworks-agent") == 60000000 * 1024

    def bad_exec(_client, _cmd, **_kw):
        return ("", "df: no such file", 1)

    monkeypatch.setattr(ssh_client, "exec", bad_exec)
    assert deploy_agent._remote_free_bytes(client, "/home/spark/.fireworks-agent") is None


def test_deploy_sync_rejects_insufficient_disk(monkeypatch, tmp_path):
    """磁盘空间不足：上传前给出明确中文报错，不进入 SFTP 上传。"""
    agent_dir = tmp_path / "agent"
    (agent_dir / "wheels" / "3.12").mkdir(parents=True)
    for name in ("main.py", "requirements.txt", "deploy.sh"):
        (agent_dir / name).write_text("x" * 100)
    (agent_dir / "wheels" / "3.12" / "dep.whl").write_bytes(b"w" * 1000)
    monkeypatch.setattr(deploy_agent, "LOCAL_AGENT_DIR", agent_dir)
    monkeypatch.setattr(deploy_agent.config, "AGENT_DEPLOY_DIR", "/opt/fireworks-agent")

    uploaded = []

    def fake_exec(_client, cmd, **_kw):
        if cmd.startswith("df -Pk"):
            return ("/dev/x 100 90 10 90% /", "", 0)  # 仅剩 10 KiB
        return ("", "", 0)

    monkeypatch.setattr(ssh_client, "connect", lambda *_a, **_k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(ssh_client, "exec", fake_exec)
    monkeypatch.setattr(ssh_client, "sftp_put", lambda *a, **k: uploaded.append(a))
    monkeypatch.setattr(ssh_client, "sftp_put_dir", lambda *a, **k: uploaded.append(a))

    result = deploy_agent._deploy_sync(SimpleNamespace(agent_port=9000), "tok_abc")
    assert result["ok"] is False
    assert "磁盘空间不足" in result["error"]
    assert "剩余 10.0 KiB" in result["error"]
    assert uploaded == []  # 未做任何上传


def test_deploy_sync_skips_check_when_df_unavailable(monkeypatch, tmp_path):
    """df 查询失败时不阻断部署（预检只做增强，不引入新的失败面）。"""
    agent_dir = tmp_path / "agent"
    (agent_dir / "wheels" / "3.12").mkdir(parents=True)
    for name in ("main.py", "requirements.txt", "deploy.sh"):
        (agent_dir / name).write_text("x" * 100)
    (agent_dir / "wheels" / "3.12" / "dep.whl").write_bytes(b"w" * 1000)
    monkeypatch.setattr(deploy_agent, "LOCAL_AGENT_DIR", agent_dir)

    calls = []

    def fake_exec(_client, cmd, **_kw):
        calls.append(cmd)
        if cmd.startswith("df -Pk"):
            return ("", "df: not found", 127)
        if "deploy.sh" in cmd and "bash" in cmd:
            return ("[deploy] 服务已就绪 (端口 9000)", "", 0)
        return ("", "", 0)

    monkeypatch.setattr(ssh_client, "connect", lambda *_a, **_k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(ssh_client, "exec", fake_exec)
    monkeypatch.setattr(ssh_client, "sftp_put", lambda *a, **k: calls.append(("put", a)))
    monkeypatch.setattr(ssh_client, "sftp_put_dir", lambda *a, **k: calls.append(("put_dir", a)))

    result = deploy_agent._deploy_sync(SimpleNamespace(agent_port=9000), "tok_abc")
    assert result["ok"] is True
    assert any(isinstance(c, tuple) and c[0] == "put" for c in calls)
