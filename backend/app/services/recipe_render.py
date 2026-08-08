"""配方渲染引擎：把配方模板 + 三类变量（cluster/node/user）渲染为逐节点的
compose.yml 与 .env（.env 由 docker compose 自动做 ${VAR} 插值，与参考仓库兼容）。

- source=cluster : 从集群自动填充（master_addr / master_port / nodes_total）
- source=node    : 按节点硬件信息自动填充（node_rank / node_roce_ip / hca / netdev / gid_index）
- source=user    : 取发布时的用户变量，缺省用 default，required 缺失则报错

高速网 IP（node_roce_ip）在集群有 network_plan 时以 **netplan 分配为准**（本项目接管
网卡配置后，实际地址由集群规划决定；hardware_info 里的 RoCE IP 可能是接管前的旧地址，
不能用作分布式 MASTER_ADDR / VLLM_HOST_IP）。
"""

from ..models import Cluster, Node, Recipe
from .network_config import node_ips


class RenderError(ValueError):
    pass


def _pick_roce(node: Node) -> dict | None:
    """取首个 RoCE 口作为 primary 兜底（无可用口时取其 netdev 等字段）。

    可用口的选择统一在 _roce_usable（LINK UP + IP + GID），此处只负责
    primary 为空时退回硬件列表首项的兜底。
    """
    hw = node.hardware_info or {}
    roce = hw.get("roce") or []
    return roce[0] if roce else None


def _roce_usable(node: Node) -> list[dict]:
    """可用 RoCE 口列表（LINK UP 且已分配 IP → 有 RoCEv2 GID + IPv4）。

    手动 enable 设计已取消（实测多口自动配置无加速增益）：默认使用全部可用 HCA。
    """
    hw = node.hardware_info or {}
    roce = hw.get("roce") or []
    return [r for r in roce if r.get("gid_index") is not None and r.get("rocev2_ip")]


def _roce_hcas(node: Node) -> str | None:
    """全部可用 RoCE HCA，逗号分隔供 NCCL 多 rail 使用。

    DGX Spark 官方（spark-clustering）布局：每个 PCIe 通道 = 一条独立 100G
    链路（独立子网），节点 2 个 QSFP 共 4 通道。NCCL_IB_HCA 填全部可用 HCA
    可同时利用全部链路（官方工具 nvidia-sync-cluster 配置后 4 口均有
    RoCEv2 GID）。单口时退化为原单 HCA 行为。
    """
    hcas = [r.get("hca") for r in _roce_usable(node)]
    return ",".join(hcas) if hcas else None


def node_auto_vars(node: Node, role: str, node_rank: int,
                   plan: dict | None = None, ip_index: int | None = None) -> dict:
    hw = node.hardware_info or {}
    # primary = 第一个可用 HCA：netdev/gid_index/node_roce_ip 都随它
    # （NCCL_SOCKET_IFNAME / NCCL_IB_GID_INDEX 必须与 NCCL_IB_HCA 选择一致）
    usable = _roce_usable(node)
    primary = usable[0] if usable else None
    roce = primary or _pick_roce(node)
    interfaces = hw.get("interfaces") or []
    physical = [i for i in interfaces if i.get("up") and i.get("ipv4")]
    netdev = roce.get("netdev") if roce else None
    if not netdev and physical:
        netdev = physical[0]["name"]

    node_roce_ip = None
    if plan and ip_index and netdev in (plan.get("iface_subnets") or {}):
        # 集群网络由本项目接管：node_roce_ip 以 netplan 分配为准，不信任接管前的
        # hardware_info RoCE IP（否则 vLLM 会把 MASTER_ADDR 绑到过期地址导致
        # Engine core init 失败）。HCA/GID 仍取硬件信息（物理属性不受影响）。
        node_roce_ip = node_ips(plan, ip_index)[netdev]
    elif roce:
        node_roce_ip = roce.get("rocev2_ip") or (roce.get("ipv4") or [None])[0]
    if not node_roce_ip:
        node_roce_ip = node.ip

    return {
        "node_rank": node_rank,
        "role": role,
        "hostname": hw.get("hostname"),
        "node_ip": node.ip,
        "node_roce_ip": node_roce_ip,
        # 多 HCA（逗号分隔，NCCL 多 rail）；单口时与原逻辑一致
        "hca": _roce_hcas(node) or (roce.get("hca") if roce else None),
        "netdev": netdev,
        "gid_index": roce.get("gid_index") if roce else None,
        "agent_port": node.agent_port,
        # worker 节点 headless（不跑 API server，mp 多节点协调必需）
        "headless": "" if role == "head" else "1",
    }


def cluster_auto_vars(cluster: Cluster, assignments) -> dict:
    """assignments: list[(Node, role, node_rank)]"""
    head = next((n for n, role, _ in assignments if role == "head"), None)
    head_rank = next((rank for _, role, rank in assignments if role == "head"), 0)
    head_vars = (
        node_auto_vars(head, "head", head_rank,
                       plan=cluster.network_plan, ip_index=head_rank + 1)
        if head else {}
    )
    return {
        "master_addr": head_vars.get("node_roce_ip") or (head.ip if head else ""),
        "master_port": cluster.master_port,
        "nodes_total": len(assignments),
        "network_type": cluster.network_type,
        "head_ip": head.ip if head else "",
        "head_hostname": head_vars.get("hostname"),
    }


def _coerce(value, var: dict) -> str:
    """按变量类型校验并返回字符串值。"""
    vtype = var.get("type", "string")
    s = str(value)
    try:
        if vtype == "int":
            int(s)
        elif vtype == "float":
            float(s)
        elif vtype == "bool":
            if s.lower() not in ("true", "false", "1", "0", "yes", "no"):
                raise ValueError
    except ValueError as e:
        raise RenderError(f"变量 {var.get('key')} 的值 '{s}' 不是合法的 {vtype}") from e
    return s


def render_task(
    recipe: Recipe,
    cluster: Cluster,
    assignments,
    user_vars: dict,
    task_name: str,
) -> dict:
    """渲染任务。assignments: list[(Node, role, node_rank)]
    返回 {"cluster_vars": {...}, "nodes": {node_id: {"project", "compose_yaml", "env", "role", "node_rank"}}}
    """
    var_defs = recipe.variables or []
    cluster_vars = cluster_auto_vars(cluster, assignments)
    cluster_node_vars = {
        n.id: node_auto_vars(n, role, rank,
                             plan=cluster.network_plan, ip_index=rank + 1)
        for n, role, rank in assignments
    }

    def resolve(var: dict, node_vars: dict | None) -> str | None:
        key = var["key"]
        # 用户显式提供的值优先（覆盖自动值）
        if key in user_vars and user_vars[key] not in (None, ""):
            return _coerce(user_vars[key], var)
        src = var.get("source", "user")
        if src == "cluster":
            auto = var.get("auto")
            val = cluster_vars.get(auto) if auto else None
        elif src == "node":
            auto = var.get("auto")
            val = (node_vars or {}).get(auto) if auto else None
        else:
            val = None
        if val in (None, ""):
            val = var.get("default")
        if val in (None, ""):
            if var.get("required"):
                raise RenderError(f"缺少必填变量 {key}")
            return None
        return _coerce(val, var)

    # 校验并构建每个节点的 env
    nodes_out = {}
    for node, role, rank in assignments:
        env = {"TASK_NAME": task_name, "NODE_ROLE": role.upper()}
        for var in var_defs:
            val = resolve(var, cluster_node_vars[node.id])
            if val is not None:
                env[var["key"]] = val
        nodes_out[str(node.id)] = {
            "project": task_name,
            "compose_yaml": recipe.compose_template,
            "env": env,
            "role": role,
            "node_rank": rank,
        }

    return {"cluster_vars": cluster_vars, "nodes": nodes_out}
