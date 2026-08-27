"""Agent 间高速直传的公共能力：权威地址选择与 Agent 能力协商。"""

from ..models import Cluster, ClusterNode, Node
from . import network_config


def validate_share_path(path: str | None) -> str:
    """校验 head Agent 返回的共享路径（worker 直拉 source_url 用）。

    只允许安全的绝对路径段，禁止 userinfo 注入（@）、引号/空白/控制字符与
    越级段——否则可构造出 http://host:port@evil.example:80/... 把 worker
    的拉取与共享 token 重定向到外部主机。
    """
    p = str(path or "")
    if not p.startswith("/") or len(p) > 1024:
        raise ValueError("head 共享路径非法：必须为绝对路径且长度受限")
    if any(ch in p for ch in ("@", "\\", " ", "'", '"', "\n", "\r", "\t")):
        raise ValueError("head 共享路径非法：含非法字符")
    if any(seg in ("", ".", "..") for seg in p.split("/")[1:]):
        raise ValueError("head 共享路径非法：含越级段")
    return p


def validate_share_token(token: str | None) -> str:
    """校验 head Agent 返回的共享 token（worker 拉取时作为授权头携带）。

    阻止控制字符注入（Authorization 头注入 / 日志注入），长度受限。
    """
    t = str(token or "")
    if len(t) > 512 or any(ch in t for ch in ("\r", "\n", "\0")):
        raise ValueError("head 共享 token 非法")
    return t


def reported_high_speed_ip(node: Node) -> str | None:
    """读取 Agent 最近一次探测到的高速网地址。"""
    for port in (node.hardware_info or {}).get("roce") or []:
        if port.get("rocev2_ip"):
            return str(port["rocev2_ip"])
    return None


def node_transfer_ip(db, node: Node) -> str:
    """优先使用集群网络规划中的权威地址，缺失时回退探测地址和管理地址。"""
    member = db.query(ClusterNode).filter(ClusterNode.node_id == node.id).first()
    cluster = db.get(Cluster, member.cluster_id) if member else None
    plan = (cluster.network_plan or {}) if cluster else {}
    if member and plan.get("iface_subnets"):
        try:
            ips = network_config.node_ips(plan, member.net_index)
            return ips.get("enp1s0f0np0") or next(iter(ips.values()))
        except (KeyError, StopIteration, ValueError):
            pass
    return reported_high_speed_ip(node) or node.ip


async def check_agent_capability(node: Node, agent_client, capability: str) -> str | None:
    """确认节点运行当前直传协议；不在传输流程中隐式修改 Agent。"""
    try:
        info = await agent_client.info(node)
    except Exception as exc:
        return f"{node.name} Agent 不可达: {exc}"
    if capability not in (info.get("capabilities") or []):
        return f"{node.name} Agent 缺少 {capability} 能力，请重新部署 Agent"
    return None
