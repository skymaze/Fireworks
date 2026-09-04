"""配方渲染参数填充回归：单高速网线下 RoCE 主口必须落在已接线的 rail 上。"""

from app.models import Node
from app.services import network_config as nc
from app.services import recipe_render as rr

PLAN = nc.plan_cluster_network("10.100.0.0/16", 9000)

# get_roce() 输出顺序：HCA 字典序、端口序号序
ROCE_ORDER = [
    ("mlx5_0", 1, "enp1s0f0np0"),
    ("mlx5_0", 2, "enp1s0f1np1"),
    ("mlx5_1", 1, "enP2p1s0f0np0"),
    ("mlx5_1", 2, "enP2p1s0f1np1"),
]


def _node(states, gid_on_all=True):
    """构造带 4 个 RoCE 口的节点；states 为该顺序下的 IB 端口 state（None=缺省）。

    gid_on_all=True 模拟最坏情况：未接线的口也残留 GID/IP（静态 IP + 派生 GID
    与链路状态无关时），仅靠 GID/IP 无法区分接线与否。
    """
    node = Node(id=2, name="n2", ip="192.0.2.2")
    hw = {"hostname": "n2"}
    hw["roce"] = []
    for i, (hca, port, netdev) in enumerate(ROCE_ORDER):
        state = states[i]
        link_up = bool(state and (state.strip().startswith("3") or state.strip().startswith("4")))
        entry = {
            "hca": hca, "port": port, "netdev": netdev,
            "link_layer": "Ethernet", "rate": "100 Gb/sec",
            "state": state,
            "gid_index": (i + 1) if (gid_on_all or link_up) else None,
            "ipv4": [nc.node_ips(PLAN, 2)[netdev]],
            "rocev2_ip": nc.node_ips(PLAN, 2)[netdev] if (gid_on_all or link_up) else None,
        }
        hw["roce"].append(entry)
    node.hardware_info = hw
    return node


def test_single_cable_picks_connected_rail():
    """只接一条线（且未接线口残留 GID/IP）时，主口必须选到已接线的 rail。"""
    # 只有 enP2p1s0f0np0（rail1）接线；其余口虽残留 GID/IP 但 state=DOWN
    states = ["1: DOWN", "1: DOWN", "4: ACTIVE", "1: DOWN"]
    node = _node(states)
    vars_ = rr.node_auto_vars(node, "worker", 1, plan=PLAN, net_index=2)
    assert vars_["netdev"] == "enP2p1s0f0np0"
    assert vars_["node_roce_ip"] == nc.node_ips(PLAN, 2)["enP2p1s0f0np0"] == "10.100.1.11"
    assert vars_["gid_index"] == 3  # 紧跟已接线口的 GID
    assert vars_["hca"] == "mlx5_1"  # NCCL_IB_HCA 只含已接线的 HCA


def test_single_cable_first_port_connected_keeps_primary():
    """接线在默认首口（rail0=enp1s0f0np0）时保持原有 primary 行为。"""
    states = ["4: ACTIVE", "1: DOWN", "1: DOWN", "1: DOWN"]
    node = _node(states)
    vars_ = rr.node_auto_vars(node, "worker", 1, plan=PLAN, net_index=2)
    assert vars_["netdev"] == "enp1s0f0np0"
    assert vars_["node_roce_ip"] == "10.100.0.11"
    assert vars_["hca"] == "mlx5_0"


def test_full_wiring_unchanged():
    """四条线全接时行为与原来一致：primary 取首个可用口，HCA 按口全量拼接。"""
    states = ["4: ACTIVE", "4: ACTIVE", "4: ACTIVE", "4: ACTIVE"]
    node = _node(states)
    vars_ = rr.node_auto_vars(node, "head", 0, plan=PLAN, net_index=2)
    assert vars_["netdev"] == "enp1s0f0np0"
    assert vars_["node_roce_ip"] == "10.100.0.11"
    assert vars_["hca"] == "mlx5_0,mlx5_0,mlx5_1,mlx5_1"  # NCCL_IB_HCA 原样按口拼接
    assert vars_["gid_index"] == 1


def test_state_missing_falls_back_to_old_behavior():
    """state 字段缺失/未解析时退回原逻辑：取首个有 GID/IP 的口。"""
    node = _node([None, None, None, None])
    vars_ = rr.node_auto_vars(node, "worker", 1, plan=PLAN, net_index=2)
    assert vars_["netdev"] == "enp1s0f0np0"
    assert vars_["node_roce_ip"] == "10.100.0.11"
    assert vars_["hca"] == "mlx5_0,mlx5_0,mlx5_1,mlx5_1"


def test_no_usable_gid_falls_back_to_connected_port():
    """没有口带 GID/IP 但有接线口时，_pick_roce 兜底到已接线的口。"""
    node = _node(["4: ACTIVE", "1: DOWN", "1: DOWN", "1: DOWN"], gid_on_all=False)
    node.hardware_info["roce"][0]["gid_index"] = None
    node.hardware_info["roce"][0]["rocev2_ip"] = None
    vars_ = rr.node_auto_vars(node, "worker", 1, plan=PLAN, net_index=2)
    assert vars_["netdev"] == "enp1s0f0np0"  # _pick_roce 兜底到硬件首口
    assert vars_["node_roce_ip"] == "10.100.0.11"
