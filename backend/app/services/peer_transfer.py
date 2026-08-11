"""Agent 间高速直传的公共能力：权威地址选择与 Agent 能力协商。"""

from ..models import Cluster, ClusterNode, Node
from . import network_config


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
    except Exception as exc:  # noqa: BLE001
        return f"{node.name} Agent 不可达: {exc}"
    if capability not in (info.get("capabilities") or []):
        return f"{node.name} Agent 缺少 {capability} 能力，请重新部署 Agent"
    return None
