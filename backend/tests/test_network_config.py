"""网络规划纯函数回归：plan / node_ips / _plan_grep / 渲染。"""

from app.models import Node
from app.services import network_config as nc


def test_plan_cluster_network_subnets():
    plan = nc.plan_cluster_network("10.100.0.0/16", 9000)
    assert plan["cidr"] == "10.100.0.0/16" and plan["mtu"] == 9000
    # 4 个接口按官方映射到 4 个相邻 /24
    assert list(plan["iface_subnets"].values()) == [
        "10.100.0.0/24", "10.100.1.0/24", "10.100.2.0/24", "10.100.3.0/24",
    ]
    assert [i for i, _ in nc.ROCE_IFACES] == list(plan["iface_subnets"].keys())


def test_plan_too_small_cidr():
    try:
        nc.plan_cluster_network("10.100.0.0/25")
    except ValueError:
        return
    raise AssertionError("应拒绝过小网段")


def test_plan_requires_four_rail_subnets_and_normalizes_cidr():
    try:
        nc.plan_cluster_network("10.100.0.0/23")
    except ValueError as exc:
        assert "/22" in str(exc)
    else:
        raise AssertionError("/23 只能容纳两个 /24 rail，应拒绝")
    assert nc.plan_cluster_network("10.100.2.3/16")["cidr"] == "10.100.0.0/16"


def test_node_ips_offsets():
    plan = nc.plan_cluster_network("10.100.0.0/16", 9000)
    ips = nc.node_ips(plan, 1)
    assert list(ips.values()) == ["10.100.0.10", "10.100.1.10", "10.100.2.10", "10.100.3.10"]
    ips2 = nc.node_ips(plan, 2)
    assert ips2["enp1s0f0np0"] == "10.100.0.11"
    # 同一子网内不同 index 不重复 / 不同接口同网段不同主机位
    assert len(set(ips.values())) == 4


def test_plan_grep_escaped_prefix():
    plan = nc.plan_cluster_network("10.200.0.0/16", 9000)
    assert nc._plan_grep(plan) == r"10\.200\."


def test_render_netplan_yaml_declares_roce_only():
    plan = nc.plan_cluster_network("10.100.0.0/16", 9000)
    y = nc._render_netplan_yaml(plan, 1)
    assert "renderer: NetworkManager" in y
    assert "dhcp4: false" in y
    assert "10.100.0.10/24" in y and "10.100.3.10/24" in y
    assert "enP7s7" not in y  # 绝不声明管理口
    assert "999-fireworks" not in y  # 纯接口声明文件，不含接管元信息


def test_detect_existing_network_preserves_node_indices(monkeypatch):
    plan = nc.plan_cluster_network("10.10.0.0/16", 9000)
    nodes = [Node(id=2, name="n2", ip="192.0.2.2"), Node(id=1, name="n1", ip="192.0.2.1")]

    def snapshot(node):
        index = node.id
        ips = nc.node_ips(plan, index)
        return {
            iface: {"addresses": [ips[iface]], "mtu": 9000, "error": ""}
            for iface, _ in nc.ROCE_IFACES
        }

    monkeypatch.setattr(nc, "inspect_node_network", snapshot)
    analysis = nc.analyze_existing_cluster_network(nodes)
    assert analysis["mode"] == "reuse"
    assert analysis["plan"] == plan
    assert analysis["node_indices"] == {2: 2, 1: 1}


def test_detect_existing_network_rejects_incomplete_layout(monkeypatch):
    node = Node(id=1, name="n1", ip="192.0.2.1")
    monkeypatch.setattr(nc, "inspect_node_network", lambda _node: {})
    assert nc.analyze_existing_cluster_network([node])["mode"] == "configure"


def test_analyze_existing_network_requires_reconfigure_for_mixed_cidrs(monkeypatch):
    nodes = [Node(id=i, name=f"n{i}", ip=f"192.0.2.{i}") for i in range(1, 5)]
    plans = {
        1: nc.plan_cluster_network("10.10.0.0/16", 9000),
        2: nc.plan_cluster_network("10.10.0.0/16", 9000),
        3: nc.plan_cluster_network("10.0.0.0/16", 9000),
        4: nc.plan_cluster_network("10.0.0.0/16", 9000),
    }

    def snapshot(node):
        plan = plans[node.id]
        index = 1 if node.id in (1, 3) else 2
        ips = nc.node_ips(plan, index)
        return {
            iface: {"addresses": [ips[iface]], "mtu": 9000, "error": ""}
            for iface, _ in nc.ROCE_IFACES
        }

    monkeypatch.setattr(nc, "inspect_node_network", snapshot)
    analysis = nc.analyze_existing_cluster_network(nodes)
    assert analysis["mode"] == "reconfigure"
    assert {network["cidr"] for network in analysis["networks"]} == {
        "10.0.0.0/16", "10.10.0.0/16",
    }


def test_physical_probe_accepts_nodes_on_different_ip_subnets(monkeypatch):
    nodes = [Node(id=1, name="n1", ip="192.0.2.1"), Node(id=2, name="n2", ip="192.0.2.2")]
    snapshots = {}
    for node, prefix, mac_prefix in ((nodes[0], "10.0", "00:00:00:00:01"),
                                     (nodes[1], "10.10", "00:00:00:00:02")):
        snapshots[node.id] = {
            iface: {
                "exists": True, "carrier": True, "operstate": "up", "mtu": 9000,
                "mac": f"{mac_prefix}:{rail:02x}",
                "addresses": [f"{prefix}.{rail}.{node.id + 9}"],
            }
            for iface, rail in nc.ROCE_IFACES
        }

    def arp(_node, targets):
        result = {}
        for target in targets:
            target_id = int(target["key"].split(":")[1])
            iface = target["iface"]
            result[target["key"]] = [snapshots[target_id][iface]["mac"]]
        return result

    monkeypatch.setattr(nc, "_active_arp_probe", arp)
    result = nc.probe_cluster_physical_links(nodes, snapshots)
    assert result["ok"] and result["status"] == "verified"
    assert len(result["links"]) == 8


def test_plan_ip_probe_reports_unexpected_arp_responder(monkeypatch):
    node = Node(id=1, name="n1", ip="192.0.2.1")
    snapshot = {
        iface: {
            "exists": True, "carrier": True, "operstate": "up", "mtu": 9000,
            "mac": f"00:00:00:00:01:{rail:02x}", "addresses": [],
        }
        for iface, rail in nc.ROCE_IFACES
    }
    monkeypatch.setattr(
        nc, "_active_arp_probe",
        lambda _node, targets: {
            target["key"]: (["de:ad:be:ef:00:01"] if target["iface"] == nc.ROCE_IFACES[0][0] else [])
            for target in targets
        },
    )
    conflicts = nc.probe_plan_ip_conflicts(
        [node], nc.plan_cluster_network("10.20.0.0/16"), {node.id: 1}, {node.id: snapshot}
    )
    assert len(conflicts) == 1
    assert conflicts[0]["ip"] == "10.20.0.10"
    assert conflicts[0]["observed_mac"] == "de:ad:be:ef:00:01"
