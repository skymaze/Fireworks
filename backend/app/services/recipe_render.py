"""配方渲染引擎：把配方模板 + 三类变量（cluster/node/user）渲染为逐节点的
compose.yml 与 .env（.env 由 docker compose 自动做 ${VAR} 插值，与参考仓库兼容）。

- source=cluster : 从任务共享自动变量填充（head_roce_ip / nodes_total 等；MASTER_PORT 属 user 变量）
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


def _roce_link_up(entry: dict) -> bool:
    """RoCE 口是否有物理链路：IB 端口 state 为 ARMED(3)/ACTIVE(4) 即有链路。

    未接线的口 state 为 DOWN；不可仅凭 GID/IP 存在判断链路（静态 IP 与派生 GID
    在未接线口上也可能存在）。state 缺失/格式异常时视为未知并回退。
    """
    state = (entry.get("state") or "").strip()
    return state.startswith(("3", "4"))


def _pick_roce(node: Node) -> dict | None:
    """取首个有物理链路的 RoCE 口作为 primary 兜底（无链接口时退回硬件列表首项）。

    可用口的选择统一在 _roce_usable（LINK UP + IP + GID），此处只负责
    primary 为空时退回硬件列表首项的兜底。
    """
    hw = node.hardware_info or {}
    roce = hw.get("roce") or []
    connected = [r for r in roce if _roce_link_up(r)]
    return (connected or roce)[0] if (connected or roce) else None


def _roce_usable(node: Node) -> list[dict]:
    """可用 RoCE 口列表（有链路优先，且已分配 IP → 有 RoCEv2 GID + IPv4）。

    手动 enable 设计已取消（实测多口自动配置无加速增益）：默认使用全部可用 HCA。
    只接一条高速网线时，仅接线的口有链路（state=ACTIVE），优先排在前面，其余
    未接线的口如果有 GID/IP 也保留（链路恢复后即可用），但不会成为 primary。
    """
    hw = node.hardware_info or {}
    roce = hw.get("roce") or []
    usable = [r for r in roce if r.get("gid_index") is not None and r.get("rocev2_ip")]
    connected = [r for r in usable if _roce_link_up(r)]
    return connected or usable


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
                   plan: dict | None = None, net_index: int | None = None) -> dict:
    """逐节点自动变量。role/node_rank 属任务级（发布时指定），net_index 为高速网槽位。

    plan 配置把 node_roce_ip 绑定到 net_index（netplan 分配），与任务 rank 无关，
    因此同一节点在不同任务里可以有各自的 head/worker/rank，而 RoCE IP 恒定。
    """
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
    if plan and net_index and netdev in (plan.get("iface_subnets") or {}):
        # 集群网络由本项目接管：node_roce_ip 以 netplan 分配为准，不信任接管前的
        # hardware_info RoCE IP（否则 vLLM 会把 MASTER_ADDR 绑到过期地址导致
        # Engine core init 失败）。HCA/GID 仍取硬件信息（物理属性不受影响）。
        node_roce_ip = node_ips(plan, net_index)[netdev]
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
    """任务级共享自动变量。assignments: list[(Node, role, node_rank)]

    head_roce_ip（供 MASTER_ADDR 填充）按任务 head 的 RoCE IP（net_index 取 plan 分配），
    与集群成员/任务 rank 解耦：head 由发布时指定且为 rank0。nodes_total 随任务节点数。
    master_port 不是集群/共享参数——MASTER_PORT 属于配方 user 变量（默认 25000）。
    """
    net_by_id = {m.node_id: m.net_index for m in cluster.members}
    head = next((n for n, role, _ in assignments if role == "head"), None)
    head_vars = (
        node_auto_vars(head, "head", 0,
                       plan=cluster.network_plan, net_index=net_by_id.get(head.id))
        if head else {}
    )
    return {
        "head_roce_ip": head_vars.get("node_roce_ip") or (head.ip if head else ""),
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


# 自动填充键白名单（source=cluster/node 的 auto 取值）。
# ★ 与 FireworksRecipes 仓库 docs/RECIPE-FORMAT.md 的《自动填充键》清单一致；
#   新增键必须同步两处，避免配方用了未知 auto 键被静默忽略。
AUTO_KEYS = frozenset({
    # cluster（任务级共享）
    "head_roce_ip", "nodes_total", "network_type", "head_ip", "head_hostname",
    # node（逐节点）
    "node_rank", "role", "hostname", "node_ip", "node_roce_ip",
    "hca", "netdev", "gid_index", "agent_port", "headless",
})


def validate_recipe_auto_keys(recipe_vars: list[dict]) -> None:
    """校验 source=cluster/node 的变量必须使用已知 auto 键；未知则报错。

    在导入/发布两处调用，避免配方用了未知 auto 键被静默忽略（获取不到值 → 空/默认）。
    """
    for v in recipe_vars:
        src = v.get("source", "user")
        if src == "user":
            if v.get("auto"):
                raise RenderError(
                    f"变量 {v.get('key')}: source=user 不应携带 auto（auto 仅用于 cluster/node）")
            continue
        auto = v.get("auto")
        if not auto:
            raise RenderError(f"变量 {v.get('key')}: source={src} 必须声明 auto 自动填充键")
        if auto not in AUTO_KEYS:
            raise RenderError(
                f"变量 {v.get('key')}: auto「{auto}」不是已知自动填充键（{sorted(AUTO_KEYS)}）")


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
    validate_recipe_auto_keys(var_defs)
    cluster_vars = cluster_auto_vars(cluster, assignments)
    net_by_id = {m.node_id: m.net_index for m in cluster.members}
    cluster_node_vars = {
        n.id: node_auto_vars(n, role, rank,
                             plan=cluster.network_plan, net_index=net_by_id.get(n.id))
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
