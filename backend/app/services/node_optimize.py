"""节点初始优化：SSH 以 root 执行 4 项优化并重启生效（幂等、单项失败不中断）。

步骤：
  1. 无线：nmcli radio wifi off + rfkill block bluetooth；
  2. 图形界面：systemctl set-default multi-user.target；
  3. docker 权限：usermod -aG docker <ssh 用户>；
  4. swap：注释 /etc/fstab 的 swap 条目（#FW-DISABLED-）+ mask 常见 swap/zram 单元；
  5. systemd-run 延迟重启，落实 swap/GUI/docker 组生效，并验证 Agent 随系统自启。

best-effort：失败或无法取得 root 不抛异常，返回 steps/warnings，不阻断添加节点。
配置均持久化；重启未调度成功时清理类效果需手动重启（见 warnings）。

提权：root 直连 -> 免密 sudo -> 密码 sudo（密码经 SSH stdin）；脚本 base64 落 /tmp 执行。
"""

import base64
import re
import uuid
from datetime import datetime, timezone

from ..models import Node, iso_utc
from . import ssh_client

# 优化脚本（root 执行）。USERNAME 在调用处经 [A-Za-z0-9_.-]+ 校验后格式化注入，
# 因此内嵌双引号是安全的。
_OPTIMIZE_SH = """#!/usr/bin/env bash
# Fireworks node initial optimization: disable wifi/bluetooth & GUI & swap, grant docker, reboot.
set +e
USERNAME="INJECT_USER"

echo "FW_STEP wireless start"
# 1) Disable Wi-Fi/Bluetooth: nmcli radio wifi off (state kept by NM); rfkill block bluetooth
#    (persisted by systemd-rfkill). Restore: nmcli radio wifi on / rfkill unblock bluetooth
if command -v nmcli >/dev/null 2>&1 && nmcli -t -f RUNNING general status 2>/dev/null | grep -qi running; then
  nmcli radio wifi off 2>/dev/null
fi
if command -v rfkill >/dev/null 2>&1; then
  rfkill block bluetooth 2>/dev/null
fi
echo "FW_STEP wireless ok"

echo "FW_STEP gui start"
# 2) Disable GUI: switch default target to multi-user.target (no graphical login after reboot)
if command -v systemctl >/dev/null 2>&1; then
  systemctl set-default multi-user.target 2>/dev/null
fi
echo "FW_STEP gui ok"

echo "FW_STEP docker start"
# 3) Grant current SSH user docker group access
if [ -z "$USERNAME" ]; then
  echo "FW_STEP docker fail no node user specified"
elif ! id "$USERNAME" >/dev/null 2>&1; then
  echo "FW_STEP docker fail user $USERNAME does not exist"
else
  if usermod -aG docker "$USERNAME" 2>/dev/null \
     && id -nG "$USERNAME" 2>/dev/null | grep -qw docker; then
    echo "FW_STEP docker ok user $USERNAME added to docker group"
  else
    echo "FW_STEP docker fail could not add $USERNAME to docker group"
  fi
fi

echo "FW_STEP swap start"
# 4) Disable swap (persistent): comment out fstab swap entries (#FW-DISABLED- prefix, reversible);
#    mask common swap/zram units (no swapoff -a: leftover is released by the reboot)
# Idempotent: strip any existing prefix first, then add exactly one; skip #-comment lines.
if [ -f /etc/fstab ]; then
  sed -i -E 's/^#FW-DISABLED-//; /^#/b; /[[:space:]]+swap[[:space:]]+/s/^/#FW-DISABLED-/' /etc/fstab 2>/dev/null
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl mask systemd-swap.service 2>/dev/null
  systemctl mask dphys-swapfile.service 2>/dev/null
  systemctl mask nvzramconfig.service 2>/dev/null
  systemctl mask zramswap.service 2>/dev/null
fi
# Count active swaps (skip header line) for the summary; leftover is released by the reboot
SWAP_COUNT=$(awk 'NR>1{n++} END{print n+0}' /proc/swaps 2>/dev/null)
if [ "${SWAP_COUNT:-0}" = "0" ]; then
  echo "FW_STEP swap ok no active swap (none re-created after reboot)"
else
  echo "FW_STEP swap ok ${SWAP_COUNT} active swap released by reboot"
fi

echo "FW_STEP reboot start"
# 5) Reboot via systemd-run: apply swap/GUI/docker changes and verify Agent auto-start
if command -v systemd-run >/dev/null 2>&1; then
  if systemd-run --on-active=2s systemctl reboot >/dev/null 2>&1; then
    echo "FW_STEP reboot ok node reboot scheduled in 2s"
  else
    echo "FW_STEP reboot fail could not schedule systemd reboot, reboot manually"
  fi
else
  echo "FW_STEP reboot fail systemd-run not found, reboot manually"
fi

echo "FW_STEP done"
"""

# 各步骤展示标签（summary 用）；注意第 5 步 reboot 是"重启生效"，其余 4 步为配置改动
_STEP_LABELS = {
    "wireless": "无线(Wi-Fi/蓝牙)",
    "gui": "图形界面",
    "docker": "Docker 权限",
    "swap": "swap",
    "reboot": "重启生效",
}
_STEP_ORDER = ("wireless", "gui", "docker", "swap", "reboot")


def _exec_as_root(node: Node, command: str, timeout: int = 180) -> tuple[int, str] | None:
    """以 root 在节点执行 command；无法提权返回 None。

    提权链：root 直连 -> 免密 sudo -> 密码 sudo（密码经 stdin，不进命令行）。
    """
    client = ssh_client.connect(node, timeout=20)
    try:
        if node.ssh_username in (None, "", "root"):
            out, err, rc = ssh_client.exec(client, f"bash -c '{command}'", timeout=timeout)
            return rc, "\n".join(p for p in (out, err) if p)
        _, _, rc0 = ssh_client.exec(client, "sudo -n true", timeout=15)
        if rc0 == 0:
            out, err, rc = ssh_client.exec(
                client, f"sudo bash -c '{command}'", timeout=timeout
            )
            return rc, "\n".join(p for p in (out, err) if p)
        if node.ssh_password:
            out, err, rc = ssh_client.exec(
                client,
                f"sudo -S -p '' bash -c '{command}'",
                timeout=timeout,
                input_data=node.ssh_password + "\n",
            )
            return rc, "\n".join(p for p in (out, err) if p)
        return None
    finally:
        client.close()


def _parse_steps(output: str) -> list[dict]:
    """解析脚本打印的 `FW_STEP <key> <status> [detail]` 行（start/done 跳过，末行生效）。"""
    entries: dict[str, dict] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("FW_STEP "):
            continue
        parts = line.split(" ", 3)
        if len(parts) < 3:
            continue
        _marker, key, status = parts[:3]
        if key in ("start", "done"):
            continue
        entries[key] = {
            "key": key,
            "ok": status == "ok",
            "detail": (parts[3].strip() if len(parts) > 3 else ""),
        }
    return [entries[k] for k in _STEP_ORDER if k in entries]


def _summarize(steps: list[dict]) -> str:
    if not steps:
        return "未获得任何优化步骤结果，请查看告警"
    parts = []
    for s in steps:
        label = _STEP_LABELS.get(s["key"], s["key"])
        parts.append(f"{label}: {'完成' if s['ok'] else '失败'}")
    return "；".join(parts)


def _run_node_script(node: Node, script: str) -> dict:
    """以 root 在节点执行嵌入脚本并解析 `FW_STEP` 结果（best-effort，永不抛异常）。

    返回 {"ok", "ran_at", "steps", "summary", "warnings"}；USERNAME 已在调用处
    按字符集校验并注入脚本。
    """
    script_b64 = base64.b64encode(script.encode()).decode()
    script_path = f"/tmp/fw_opt_{uuid.uuid4().hex}.sh"
    # 内联命令不含单引号（脚本与用户都经 base64/格式化注入），可安全包进 bash -c '...'
    command = (
        f"echo {script_b64} | base64 -d > {script_path} && "
        f"bash {script_path}; rc=$?; rm -f {script_path}; exit $rc"
    )
    now = iso_utc(datetime.now(timezone.utc))
    try:
        executed = _exec_as_root(node, command, timeout=180)
    except Exception as e:  # noqa: BLE001 - SSH 连接/提权/断连等，收敛为结构化失败
        return {
            "ok": False,
            "ran_at": now,
            "steps": [],
            "summary": "SSH 执行失败",
            "warnings": [f"SSH 执行初始优化失败: {e}"],
        }
    if executed is None:
        return {
            "ok": False,
            "ran_at": now,
            "steps": [],
            "summary": "无法取得节点 root 权限，未执行",
            "warnings": ["无法取得节点 root 权限（需以 root 登录或提供 sudo 权限）"],
        }
    rc, output = executed
    steps = _parse_steps(output or "")
    if not steps:
        detail = "\n".join(part for part in (output or "",) if part)[-400:]
        return {
            "ok": False,
            "ran_at": now,
            "steps": [],
            "summary": "执行异常（脚本未返回步骤结果）",
            "warnings": [f"脚本未返回结构化结果，rc={rc}：{detail}"],
        }
    warnings = [s["detail"] for s in steps if not s["ok"]]
    if rc != 0:
        warnings.append(f"脚本退出码非零（rc={rc}），结果可能不完整")
    return {
        "ok": True,
        "ran_at": now,
        "steps": steps,
        "summary": _summarize(steps),
        "warnings": warnings,
    }


def optimize_node(node: Node) -> dict:
    """对节点执行初始优化，返回结构化结果（best-effort，永不抛异常）。

    4 项系统级优化 + 延迟重启；未能调度重启时对普通用户的 docker 权限等附加提示。
    """
    user = (node.ssh_username or "root").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", user):
        return {
            "ok": False,
            "ran_at": iso_utc(datetime.now(timezone.utc)),
            "steps": [],
            "summary": "节点用户非法，跳过初始优化",
            "warnings": [f"节点 SSH 用户非法，拒绝执行优化: {user}"],
        }
    result = _run_node_script(node, _OPTIMIZE_SH.replace("INJECT_USER", user))
    reboot_step = next((s for s in result["steps"] if s["key"] == "reboot"), None)
    # 未能确认重启时提示受影响项：普通用户已运行进程（Agent）用不上新 docker 组。
    if result["ok"] and not (reboot_step and reboot_step["ok"]):
        docker_step = next((s for s in result["steps"] if s["key"] == "docker"), None)
        if user != "root" and docker_step and docker_step["ok"]:
            result["warnings"].append(
                "未能确认重启：Docker 组对该用户已运行的进程（如 Agent）在重新登录/"
                "重启用户会话前不生效，且 swap/图形界面的持久化需重启核对"
            )
        if not docker_step:
            result["warnings"].append(
                "未能确认重启：部分优化项的生效（尤其 docker 组）有待重启核实"
            )
    return result
