"""Agent 容器化一键部署（参考 Portainer Agent）。

流程：SSH 预检（docker 可用 + 节点架构）-> 控制平面拉取 agent 镜像
（skopeo docker-archive，按架构缓存）-> 分块上传 tar -> 节点 docker load
+ docker run（挂载 docker.sock / 宿主工具链 / 数据目录，--restart 保活）
-> 连通性验证。节点只需 docker（root 或 docker 组权限），无 pip/venv/systemd。
"""

import asyncio
import hashlib
import re
import secrets
from pathlib import Path

from .. import config
from ..db import SessionLocal
from ..models import Node
from . import agent_client, image_manager, ssh_client

AGENT_FILES = ["deploy-container.sh"]

# backend/app/services/deploy_agent.py -> parents[3] = 项目根目录（开发机）；
# 容器内镜像把 agent 目录放 /app/agent（parents[2]），两处都探测。
_LOCAL_AGENT_DIR = Path(__file__).resolve().parents[3] / "agent"
if not (_LOCAL_AGENT_DIR / "deploy-container.sh").exists():
    _LOCAL_AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
LOCAL_AGENT_DIR = _LOCAL_AGENT_DIR

# 节点 uname -m -> 镜像/挂载用的架构名
_ARCH_MAP = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}


def _resolve_remote_dir(client, remote_dir: str) -> str:
    """确认部署目录可写，否则回退到 $HOME/.fireworks-agent。"""
    out, _, rc = ssh_client.exec(client, f"mkdir -p {remote_dir} && test -w {remote_dir}")
    if rc == 0:
        return remote_dir
    out, _, rc = ssh_client.exec(client, "echo $HOME/.fireworks-agent")
    home_dir = out.strip() if rc == 0 else "~/.fireworks-agent"
    ssh_client.exec(client, f"mkdir -p {home_dir}")
    return home_dir


def _agent_archive_path(arch: str) -> Path:
    """agent 镜像 tar 缓存：按架构 + 镜像引用哈希命名。

    镜像引用（AGENT_IMAGE，含 tag/registry）变化时自动换缓存重新拉取，
    不依赖手动删缓存。
    """
    digest = hashlib.sha256(config.AGENT_IMAGE.encode()).hexdigest()[:8]
    return image_manager.IMAGE_CACHE_DIR / f"agent-{digest}-{arch}.tar"


def _deploy_sync(node: Node, token: str) -> dict:
    missing = [f for f in AGENT_FILES if not (LOCAL_AGENT_DIR / f).exists()]
    if missing:
        return {"ok": False, "error": f"控制平面缺少 Agent 部署文件: {', '.join(missing)}"}
    # token 与镜像名会拼进远端命令行（bash 解析期可见/可注入），拼接前校验字符集
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return {"ok": False, "error": "Agent token 含非法字符（仅允许字母数字 - _），拒绝部署"}
    if not re.fullmatch(r"[A-Za-z0-9:./_-]+", config.AGENT_IMAGE):
        return {"ok": False, "error": "AGENT_IMAGE 含非法字符，拒绝部署"}
    client = ssh_client.connect(node)
    try:
        # 1) 预检：docker 可用（容器化部署硬前提）+ 节点架构
        out, err, rc = ssh_client.exec(
            client, "docker version --format '{{.Server.Version}}'", timeout=30
        )
        if rc != 0:
            return {
                "ok": False,
                "error": "节点 docker 不可用（需 root 或 docker 组权限）: " + (err or out)[:200],
            }
        out, _, rc = ssh_client.exec(client, "uname -m", timeout=15)
        arch = _ARCH_MAP.get((out or "").strip())
        if not arch:
            return {"ok": False, "error": f"不支持的节点架构: {(out or '').strip() or '未知'}"}
        # 2) agent 镜像 tar：控制平面缓存，缺失则按架构从 GHCR 拉取
        tar_path = _agent_archive_path(arch)
        if not tar_path.exists() or tar_path.stat().st_size == 0:
            try:
                image_manager.pull_image(config.AGENT_IMAGE, tar_path, arch=arch)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"拉取 Agent 镜像失败: {e}"}
        # 3) 上传部署脚本 + 镜像 tar（sftp 分块 + 断点续传）
        remote_dir = _resolve_remote_dir(client, config.AGENT_DEPLOY_DIR)
        for f in AGENT_FILES:
            ssh_client.sftp_put(client, str(LOCAL_AGENT_DIR / f), f"{remote_dir}/{f}")
        ssh_client.sftp_put(client, str(tar_path), f"{remote_dir}/agent-image.tar")
        # 4) 执行容器化部署脚本（docker load + docker run + 健康等待）
        out, err, rc = ssh_client.exec(
            client,
            f"FW_AGENT_TOKEN='{token}' bash {remote_dir}/deploy-container.sh "
            f"{node.agent_port} {remote_dir} {arch} '{config.AGENT_IMAGE}'",
            timeout=900,
        )
        if rc != 0:
            return {"ok": False, "error": err or out}
        return {"ok": True, "install_dir": remote_dir}
    finally:
        client.close()


def _agent_data_dir(client) -> str:
    """探测 agent 数据目录（与 _resolve_remote_dir 同逻辑，供 .ssh 互信路径使用）。"""
    out, _, rc = ssh_client.exec(
        client,
        f"test -w {config.AGENT_DEPLOY_DIR} && echo {config.AGENT_DEPLOY_DIR} "
        "|| echo $HOME/.fireworks-agent",
        timeout=15,
    )
    return (out or "").strip() or "~/.fireworks-agent"


def ensure_ssh_trust(from_node: Node, to_node: Node) -> tuple[bool, str]:
    """配置 from_node → to_node 的 SSH 免密（把 from 的公钥加入 to 的 authorized_keys）。

    控制平面作为中介：读 from 的公钥 -> 写入 to 的 authorized_keys。
    agent 容器内 ssh/rsync 使用数据目录 .ssh（容器 $HOME=/data 挂载），
    互信读写都在数据目录进行。幂等：重复执行追加前先剔除旧条目。
    """
    try:
        fclient = ssh_client.connect(from_node, timeout=20)
        try:
            fdir = _agent_data_dir(fclient)
            out, err, rc = ssh_client.exec(
                fclient, f"cat {fdir}/.ssh/id_ed25519.pub 2>/dev/null", timeout=15
            )
            pub = (out or "").strip()
        finally:
            fclient.close()
        if not pub or rc != 0:
            return False, f"节点 {from_node.name} 无 SSH 公钥（请先部署 Agent）"
        tclient = ssh_client.connect(to_node, timeout=20)
        try:
            tdir = _agent_data_dir(tclient)
            cmd = (
                f"mkdir -p {tdir}/.ssh && chmod 700 {tdir}/.ssh && "
                f"touch {tdir}/.ssh/authorized_keys && chmod 600 {tdir}/.ssh/authorized_keys && "
                f"grep -vF '{pub}' {tdir}/.ssh/authorized_keys > {tdir}/.ssh/authorized_keys.tmp && "
                f"mv {tdir}/.ssh/authorized_keys.tmp {tdir}/.ssh/authorized_keys && "
                f"echo '{pub}' >> {tdir}/.ssh/authorized_keys"
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
    """部署 Agent 到节点并验证。返回 {"ok", "hardware_info"?, "warning"?, "error"?}

    部署即轮换：每次部署生成新的节点 token 并注入；部署成功立即落库
    （失败不落库，旧 token 保持、旧 Agent 继续工作），随后用新 token 验证连通。
    """
    token = secrets.token_urlsafe(32)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _deploy_sync, node, token)
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
