"""Agent 一键部署：SSH 上传文件 -> venv 安装依赖 -> systemd/systemd --user 启动 -> 连通性验证。

用户态部署走 systemd --user + enable-linger（开机自启）；不满足 systemd 前提时部署明确失败。
支持非 root 用户部署：若配置的部署目录不可写，自动回退到 $HOME/.fireworks-agent。
"""

import asyncio
import re
import secrets
from pathlib import Path

from .. import config
from ..db import SessionLocal
from ..models import Node
from . import agent_client, ssh_client

AGENT_FILES = ["main.py", "requirements.txt", "deploy.sh"]

# backend/app/services/deploy_agent.py -> parents[3] = 项目根目录（开发机）；
# 容器内镜像把 agent 目录放 /app/agent（parents[2]），两处都探测。
_LOCAL_AGENT_DIR = Path(__file__).resolve().parents[3] / "agent"
if not (_LOCAL_AGENT_DIR / "deploy.sh").exists():
    _LOCAL_AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
LOCAL_AGENT_DIR = _LOCAL_AGENT_DIR


def _resolve_remote_dir(client, remote_dir: str) -> str:
    """确认部署目录可写，否则回退到 $HOME/.fireworks-agent。"""
    out, _, rc = ssh_client.exec(client, f"mkdir -p {remote_dir} && test -w {remote_dir}")
    if rc == 0:
        return remote_dir
    out, _, rc = ssh_client.exec(client, "echo $HOME/.fireworks-agent")
    home_dir = out.strip() if rc == 0 else "~/.fireworks-agent"
    ssh_client.exec(client, f"mkdir -p {home_dir}")
    return home_dir


def _deploy_sync(node: Node, token: str) -> dict:
    missing = [f for f in AGENT_FILES if not (LOCAL_AGENT_DIR / f).exists()]
    if missing:
        return {"ok": False, "error": f"控制平面缺少 Agent 安装文件: {', '.join(missing)}"}
    wheels_dir = LOCAL_AGENT_DIR / "wheels"
    if not wheels_dir.is_dir() or not any(wheels_dir.iterdir()):
        return {"ok": False, "error": "控制平面缺少离线依赖包（agent/wheels/，由 backend 镜像构建时生成）"}
    client = ssh_client.connect(node)
    try:
        remote_dir = _resolve_remote_dir(client, config.AGENT_DEPLOY_DIR)
        for f in AGENT_FILES:
            ssh_client.sftp_put(client, str(LOCAL_AGENT_DIR / f), f"{remote_dir}/{f}")
        # 离线依赖 wheelhouse（wheels/<py版本>/ 子目录，deploy.sh 按节点 Python 版本选用）
        ssh_client.sftp_put_dir(client, str(wheels_dir), f"{remote_dir}/wheels")
        ssh_client.exec(client, f"chmod +x {remote_dir}/deploy.sh")
        # 以环境变量把该节点的 token 传给 deploy.sh，deploy.sh 写入 Agent 启动环境。
        # token 会拼进远端命令行（bash 解析期可见/可注入），拼接前按 deploy.sh
        # 同款字符集校验（token_urlsafe 生成值天然合规，此校验为防御性）。
        if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            return {"ok": False, "error": "Agent token 含非法字符（仅允许字母数字 - _），拒绝部署"}
        out, err, rc = ssh_client.exec(
            client,
            f"FW_AGENT_TOKEN='{token}' bash {remote_dir}/deploy.sh {node.agent_port} {remote_dir}",
            timeout=600,
        )
        if rc != 0:
            return {"ok": False, "error": err or out}
        return {"ok": True, "install_dir": remote_dir}
    finally:
        client.close()


async def deploy(node: Node) -> dict:
    """部署 Agent 到节点并验证。返回 {"ok", "hardware_info"?, "warning"?, "error"?}

    部署即轮换：每次部署生成新的节点 token 并注入；部署成功立即落库
    （失败不落库，现有 token 与运行中的 Agent 保持不变），随后用新 token 验证连通。
    """
    token = secrets.token_urlsafe(32)
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _deploy_sync, node, token)
    except Exception as e:  # noqa: BLE001 - SSH 连接/上传/脚本执行等异常，统一归为部署失败返回
        # _deploy_sync 在 SSH 连接失败/传输中断等情况下会抛出而非返回失败结果；
        # 此处兜底转成 {"ok": False}，保证 deploy() 的契约：任何失败都返回失败字典，
        # 上层（添加节点/手动重部署）据此给出清晰的结构化报错而非裸 500。
        return {"ok": False, "error": str(e)}
    if not result["ok"]:
        return result
    with SessionLocal() as db:
        row = db.get(Node, node.id)
        if row is None:
            return {"ok": False, "error": "节点已不存在"}
        row.agent_token = token
        db.commit()
    node.agent_token = token  # 同步内存对象，info() 验证即用新 token
    try:
        hw = await agent_client.info(node)
        return {"ok": True, "install_dir": result.get("install_dir"), "hardware_info": hw}
    except Exception as e:  # noqa: BLE001
        return {
            "ok": True,
            "install_dir": result.get("install_dir"),
            "warning": f"部署完成但 Agent 连通性验证失败: {e}",
        }


# 卸载脚本：停止并删除 Agent 的全部足迹（systemd 系统/用户服务、工作目录、linger）
_UNINSTALL_SH = """set +e
H=$(getent passwd {user} | cut -d: -f6)
pkill -u {user} -f 'uvicorn main:app' 2>/dev/null
pkill -u {user} -f fireworks-agent 2>/dev/null
runuser -u {user} -- systemctl --user stop fireworks-agent 2>/dev/null
runuser -u {user} -- systemctl --user disable fireworks-agent 2>/dev/null
systemctl stop fireworks-agent 2>/dev/null
systemctl disable fireworks-agent 2>/dev/null
rm -f /etc/systemd/system/fireworks-agent.service
rm -f $H/.config/systemd/user/fireworks-agent.service
loginctl disable-linger {user} 2>/dev/null
rm -rf /opt/fireworks-agent $H/.fireworks-agent $H/.cache/fireworks-agent
systemctl daemon-reload 2>/dev/null
echo AGENT_UNINSTALLED
"""


def _uninstall_sync(node: Node) -> tuple[bool, str]:
    """SSH（sudo -S 密码注入）停止并删除节点上的 Agent 及工作目录。

    脚本经 base64 落到 /tmp 再执行（脚本内含单引号，直接拼进 bash -c '...'
    会破坏引号边界）。
    """
    import base64

    user = node.ssh_username or "spark"
    # 深度防御：user 会被 .format() 直接注入 root 卸载脚本（pkill -u/runuser/
    # loginctl/rm），即使 API 层已校验，这里也拒绝任何越出 POSIX 用户名字符集
    # 的值，防止配置数据异常演变为节点上以 root 执行的命令注入。
    if not re.fullmatch(r"[a-z_][a-z0-9._-]*", user or ""):
        return False, f"节点 SSH 用户非法，拒绝执行卸载: {user!r}"
    script_b64 = base64.b64encode(_UNINSTALL_SH.format(user=user).encode()).decode()
    pwd_b64 = base64.b64encode((node.ssh_password or "").encode()).decode()
    cmd = (
        "bash -c \"echo " + script_b64 + " | base64 -d > /tmp/fw_uninstall.sh && chmod +x /tmp/fw_uninstall.sh && "
        "echo " + pwd_b64 + " | base64 -d | sudo -S -k bash /tmp/fw_uninstall.sh\""
    )
    client = ssh_client.connect(node, timeout=20)
    try:
        out, err, _ = ssh_client.exec(client, cmd, timeout=180)
    finally:
        client.close()
    if "AGENT_UNINSTALLED" in out:
        return True, "已停止并删除 Agent（systemd 服务 + 工作目录）"
    return False, (err or out)[-300:] or "卸载脚本未返回预期结果"


async def uninstall(node: Node) -> dict:
    """删除节点时卸载 Agent。返回 {"ok", "msg"?, "warning"?, "error"?}"""
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(None, _uninstall_sync, node)
    if ok:
        return {"ok": True, "msg": msg}
    return {"ok": False, "error": msg, "warning": f"Agent 卸载失败（节点即将从管理删除，残留需人工清理）：{msg}"}
