"""节点初始优化服务单测：SSH 提权路径 / FW_STEP 解析 / best-effort 语义（无真实节点）。

覆盖：
- root 用户直连执行，脚本解析 4 步结果，用户按校验后注入脚本；
- 普通用户：免密 sudo 与密码 sudo（sudo -S，密码走 stdin）两条提权路径；
- 无任何提权能力 → ok=False + 警告（不抛异常）；
- 脚本未返回结构化结果（rc!=0 / 无 FW_STEP）→ ok=False + 警告；
- docker 单项失败 → 进 steps.fail 与 warnings；普通用户成功的 docker 附加即时生效提示；
- 非法节点用户 → 拒绝执行。
"""

import base64
from types import SimpleNamespace

from app.models import Node
from app.services import node_optimize, ssh_client

_FULL_OK = (
    "FW_STEP wireless start\n"
    "FW_STEP wireless ok\n"
    "FW_STEP gui start\n"
    "FW_STEP gui ok\n"
    "FW_STEP docker start\n"
    "FW_STEP docker ok user spark added to docker group\n"
    "FW_STEP swap start\n"
    "FW_STEP swap ok no active swap (none re-created after reboot)\n"
    "FW_STEP reboot start\n"
    "FW_STEP reboot ok node reboot scheduled in 2s\n"
    "FW_STEP done\n"
)


def _node(**kw) -> Node:
    base = dict(id=1, name="n1", ip="10.0.0.9", ssh_username="root",
                ssh_auth_type="password", ssh_password="pw")
    base.update(kw)
    return Node(**base)


def _patch_ssh(monkeypatch, respond):
    """mock ssh_client：connect 返回哑客户端；exec 按命令返回。

    respond: fn(command, input_data) -> (out, err, rc)
    """
    monkeypatch.setattr(
        ssh_client, "connect",
        lambda node, timeout=15: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(ssh_client, "exec", respond)


def test_root_user_runs_directly_and_parses_steps(monkeypatch):
    """root 直连：走 bash -c 而非 sudo；脚本内用户按校验后注入；4 步解析为 ok。"""
    calls: list[tuple] = []

    def respond(client, command, timeout=60, input_data=None):
        calls.append((command, input_data))
        return _FULL_OK, "", 0

    _patch_ssh(monkeypatch, respond)
    result = node_optimize.optimize_node(_node(ssh_username="root"))
    assert result["ok"] is True
    assert result["warnings"] == []  # root 且重启已调度：无需重登提示
    assert [s["key"] for s in result["steps"]] == ["wireless", "gui", "docker", "swap", "reboot"]
    assert all(s["ok"] for s in result["steps"])
    assert "无线" in result["summary"] and "swap" in result["summary"] and "重启" in result["summary"]
    # 只执行一次且为 bash -c 直跑（无 sudo）
    assert len(calls) == 1
    assert "sudo" not in calls[0][0]
    # 用户注入到 base64 脚本内（命令明文不含用户名）
    b64 = calls[0][0].split("echo ")[1].split(" | ")[0]
    decoded = base64.b64decode(b64).decode()
    assert 'USERNAME="root"' in decoded
    assert "INJECT_USER" not in decoded


def test_passwordless_sudo_path(monkeypatch):
    """普通用户 + 免密 sudo：先 sudo -n true，再 sudo bash -c 执行。"""
    calls: list[tuple] = []

    def respond(client, command, timeout=60, input_data=None):
        calls.append(command)
        if command == "sudo -n true":
            return "", "", 0
        return _FULL_OK, "", 0

    _patch_ssh(monkeypatch, respond)
    result = node_optimize.optimize_node(_node(ssh_username="spark"))
    assert result["ok"] is True
    assert calls[0] == "sudo -n true"
    assert calls[1].startswith("sudo bash -c 'echo ")
    # 重启已调度：普通用户 + docker ok 也不再提示"重登生效"
    assert not any("重新登录" in w for w in result["warnings"])
    # 密码经 stdin 而不出现在命令行
    assert "pw" not in calls[1]


def test_password_sudo_path_pipes_password_via_stdin(monkeypatch):
    """普通用户无免密 sudo 但有密码：sudo -S -p ''，密码经 input_data 而非命令行。"""
    received: list[tuple] = []

    def respond(client, command, timeout=60, input_data=None):
        received.append((command, input_data))
        if command == "sudo -n true":
            return "", "", 1  # 免密失败
        return _FULL_OK, "", 0

    _patch_ssh(monkeypatch, respond)
    result = node_optimize.optimize_node(_node(ssh_username="spark", ssh_password="s3cret"))
    assert result["ok"] is True
    assert received[-1][0].startswith("sudo -S -p '' bash -c 'echo ")
    assert received[-1][1] == "s3cret\n"
    assert "s3cret" not in received[-1][0]


def test_no_root_ability_returns_warning_not_exception(monkeypatch):
    """无免密 sudo 且无密码：返回 ok=False + 警告，不抛异常（调用方可继续保留节点）。"""
    def respond(client, command, timeout=60, input_data=None):
        return "", "", 1

    _patch_ssh(monkeypatch, respond)
    result = node_optimize.optimize_node(
        _node(ssh_username="spark", ssh_password=None)
    )  # 无密码亦无免密 sudo
    assert result["ok"] is False
    assert any("root" in w for w in result["warnings"])
    assert result["steps"] == []


def test_unparseable_output_returns_warning(monkeypatch):
    """脚本未返回 FW_STEP（异常/被系统拦截）：ok=False + 警告，含 rc 与输出片段。"""
    def respond(client, command, timeout=60, input_data=None):
        if command == "sudo -n true":
            return "", "", 0
        return "bash: line 3: systemctl: command not found\n", "", 3

    _patch_ssh(monkeypatch, respond)
    result = node_optimize.optimize_node(_node(ssh_username="root"))
    assert result["ok"] is False
    assert any("未返回结构化结果" in w for w in result["warnings"])


def test_docker_step_failure_lands_in_warnings_and_steps(monkeypatch):
    """docker 单项失败：进对应 step.ok=False 与 warnings，其余步骤不受影响。"""
    out = _FULL_OK.replace("FW_STEP docker ok user spark added to docker group",
                           "FW_STEP docker fail could not add spark to docker group")

    def respond(client, command, timeout=60, input_data=None):
        return out, "", 0

    _patch_ssh(monkeypatch, respond)
    result = node_optimize.optimize_node(_node())
    assert result["ok"] is True  # 执行成功，项目级 best-effort
    assert {s["ok"] for s in result["steps"]} == {True, False}
    assert any(s["key"] == "docker" and not s["ok"] for s in result["steps"])
    assert any("could not add spark" in w for w in result["warnings"])


def test_reboot_failure_shows_caveat_for_user_space_docker(monkeypatch):
    """reboot 步骤失败（无法调度重启）时：普通用户 + docker ok → 提示需重登/重启会话。"""
    out = _FULL_OK.replace(
        "FW_STEP reboot ok node reboot scheduled in 2s",
        "FW_STEP reboot fail systemd-run not found, reboot manually",
    )

    def respond(client, command, timeout=60, input_data=None):
        return out, "", 0

    _patch_ssh(monkeypatch, respond)
    result = node_optimize.optimize_node(_node(ssh_username="spark"))
    assert result["ok"] is True
    assert not any(s["key"] == "reboot" and s["ok"] for s in result["steps"])
    caveats = "；".join(result["warnings"])
    assert "重新登录" in caveats  # 未重启：docker 组对已运行进程（Agent）不生效


def test_invalid_username_rejected(monkeypatch):
    """非法节点用户（含 shell 元字符）→ 拒绝执行并告警，不触网。"""
    touched: list[str] = []

    def respond(client, command, timeout=60, input_data=None):
        touched.append(command)
        return "", "", 0

    _patch_ssh(monkeypatch, respond)
    result = node_optimize.optimize_node(_node(ssh_username="spark;rm -rf /"))
    assert result["ok"] is False
    assert any("非法" in w for w in result["warnings"])
    assert touched == []


def test_ssh_connect_error_returns_warning(monkeypatch):
    """SSH 连接失败（不可达/超时）：best-effort 返回 ok=False + 警告，不抛异常。"""
    def boom(node, timeout=15):
        raise TimeoutError("ssh connect timeout")

    monkeypatch.setattr(ssh_client, "connect", boom)
    result = node_optimize.optimize_node(_node())
    assert result["ok"] is False
    assert result["steps"] == []
    assert any("SSH" in w for w in result["warnings"])

