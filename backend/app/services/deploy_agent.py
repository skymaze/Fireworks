"""Agent 一键部署：SSH 上传文件 -> venv 安装依赖 -> systemd/nohup 启动 -> 连通性验证。

支持非 root 用户部署：若配置的部署目录不可写，自动回退到 $HOME/.dgx-agent。
"""

import asyncio
import re
from pathlib import Path

from .. import config
from ..models import Node
from ..security import get_agent_token
from . import agent_client, ssh_client

AGENT_FILES = ["main.py", "requirements.txt", "deploy.sh"]

# backend/app/services/deploy_agent.py -> parents[3] = 项目根目录（开发机）；
# 容器内镜像把 agent 目录放 /app/agent（parents[2]），两处都探测。
_LOCAL_AGENT_DIR = Path(__file__).resolve().parents[3] / "agent"
if not (_LOCAL_AGENT_DIR / "deploy.sh").exists():
    _LOCAL_AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
LOCAL_AGENT_DIR = _LOCAL_AGENT_DIR


def _resolve_remote_dir(client, remote_dir: str) -> str:
    """确认部署目录可写，否则回退到 $HOME/.dgx-agent。"""
    out, _, rc = ssh_client.exec(client, f"mkdir -p {remote_dir} && test -w {remote_dir}")
    if rc == 0:
        return remote_dir
    out, _, rc = ssh_client.exec(client, "echo $HOME/.dgx-agent")
    home_dir = out.strip() if rc == 0 else "~/.dgx-agent"
    ssh_client.exec(client, f"mkdir -p {home_dir}")
    return home_dir


def _deploy_sync(node: Node) -> dict:
    missing = [f for f in AGENT_FILES if not (LOCAL_AGENT_DIR / f).exists()]
    if missing:
        return {"ok": False, "error": f"控制平面缺少 Agent 安装文件: {', '.join(missing)}"}
    client = ssh_client.connect(node)
    try:
        remote_dir = _resolve_remote_dir(client, config.AGENT_DEPLOY_DIR)
        for f in AGENT_FILES:
            ssh_client.sftp_put(client, str(LOCAL_AGENT_DIR / f), f"{remote_dir}/{f}")
        ssh_client.exec(client, f"chmod +x {remote_dir}/deploy.sh")
        # 以环境变量把共享 token 传给 deploy.sh，deploy.sh 写入 Agent 启动环境。
        # token 会拼进远端命令行（bash 解析期可见/可注入），拼接前按 deploy.sh
        # 同款字符集校验，拦掉运维自定义 AGENT_TOKEN 中的引号/元字符。
        token = get_agent_token()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            return {"ok": False, "error": "AGENT_TOKEN 含非法字符（仅允许字母数字 - _），拒绝部署"}
        out, err, rc = ssh_client.exec(
            client,
            f"DGX_AGENT_TOKEN='{token}' bash {remote_dir}/deploy.sh {node.agent_port} {remote_dir}",
            timeout=600,
        )
        if rc != 0:
            return {"ok": False, "error": err or out}
        # 确保节点存在 SSH 密钥对（head→worker 镜像/模型 rsync 免密互信需要）
        ssh_client.exec(
            client,
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 -q",
            timeout=60,
        )
        return {"ok": True, "install_dir": remote_dir}
    finally:
        client.close()


def ensure_ssh_trust(from_node: Node, to_node: Node) -> tuple[bool, str]:
    """配置 from_node → to_node 的 SSH 免密（把 from 的公钥加入 to 的 authorized_keys）。

    控制平面作为中介：读 from 的公钥 -> 写入 to 的 ~/.ssh/authorized_keys。
    幂等：重复执行追加前先剔除旧条目。返回 (ok, 说明)。
    """
    try:
        fclient = ssh_client.connect(from_node, timeout=20)
        try:
            out, err, rc = ssh_client.exec(fclient, "cat ~/.ssh/id_ed25519.pub 2>/dev/null", timeout=15)
            pub = (out or "").strip()
        finally:
            fclient.close()
        if not pub or rc != 0:
            return False, f"节点 {from_node.name} 无 SSH 公钥（请先部署 Agent）"
        tclient = ssh_client.connect(to_node, timeout=20)
        try:
            # 幂等：剔除已存在的相同公钥行再追加
            cmd = (
                "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
                f"grep -vF '{pub}' ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp && "
                "mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys && "
                f"echo '{pub}' >> ~/.ssh/authorized_keys"
            )
            out, err, rc = ssh_client.exec(tclient, cmd, timeout=20)
            if rc != 0:
                return False, f"写入 {to_node.name} authorized_keys 失败: {(err or out)[:200]}"
        finally:
            tclient.close()
        return True, f"{from_node.name} → {to_node.name} 免密已配置"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def deploy(node: Node) -> dict:
    """部署 Agent 到节点并验证。返回 {"ok", "hardware_info"?, "warning"?, "error"?}"""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _deploy_sync, node)
    if not result["ok"]:
        return result
    try:
        hw = await agent_client.info(node)
        return {"ok": True, "install_dir": result.get("install_dir"), "hardware_info": hw}
    except Exception as e:  # noqa: BLE001
        return {
            "ok": True,
            "install_dir": result.get("install_dir"),
            "warning": f"部署完成但 Agent 连通性验证失败: {e}",
        }
