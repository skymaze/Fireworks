"""网络规划纯函数回归：plan / node_ips / 渲染 / 物理链路预检与单线验证。"""

import re

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


def test_render_netplan_yaml_declares_roce_only():
    plan = nc.plan_cluster_network("10.100.0.0/16", 9000)
    y = nc._render_netplan_yaml(plan, 1)
    assert "renderer: NetworkManager" in y
    assert "dhcp4: false" in y
    assert "10.100.0.10/24" in y and "10.100.3.10/24" in y
    assert "enP7s7" not in y  # 绝不声明管理口
    assert "999-fireworks" not in y  # 纯接口声明文件，不含接管元信息


def test_detect_snapshot_network_preserves_node_index():
    plan = nc.plan_cluster_network("10.10.0.0/16", 9000)
    node = Node(id=2, name="n2", ip="192.0.2.2")
    ips = nc.node_ips(plan, node.id)
    snapshot = {
        iface: {"addresses": [ips[iface]], "mtu": 9000, "error": ""}
        for iface, _ in nc.ROCE_IFACES
    }
    profile = nc._detect_snapshot_network(snapshot)
    assert profile is not None and profile["plan"] == plan and profile["index"] == 2


def test_detect_snapshot_network_rejects_incomplete_layout():
    assert nc._detect_snapshot_network({}) is None


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


def _single_cable_snapshots(nodes, cable_rail=0, with_addrs=True):
    """构造每节点仅一条高速网线接通、其余口 carrier=0 的快照。"""
    rail_iface = nc.ROCE_IFACES[cable_rail][0]
    snapshots = {}
    for node, mac_prefix in ((nodes[0], "00:00:00:00:01"), (nodes[1], "00:00:00:00:02")):
        snapshots[node.id] = {}
        for iface, rail in nc.ROCE_IFACES:
            if iface == rail_iface:
                snapshots[node.id][iface] = {
                    "exists": True, "carrier": True, "operstate": "up", "mtu": 9000,
                    "mac": f"{mac_prefix}:{rail:02x}",
                    "addresses": [f"10.0.{rail}.{node.id + 9}"] if with_addrs else [],
                }
            else:
                snapshots[node.id][iface] = {
                    "exists": True, "carrier": False, "operstate": "down", "mtu": 9000,
                    "mac": f"{mac_prefix}:{rail:02x}", "addresses": [],
                }
    return snapshots


def test_physical_probe_accepts_single_cable(monkeypatch):
    """只接一条高速网线允许建集群：未接线口仅告警，不判失败。"""
    nodes = [Node(id=1, name="n1", ip="192.0.2.1"), Node(id=2, name="n2", ip="192.0.2.2")]
    snapshots = _single_cable_snapshots(nodes)

    def arp(_node, targets):
        result = {}
        for target in targets:
            target_id = int(target["key"].split(":")[1])
            result[target["key"]] = [snapshots[target_id][target["iface"]]["mac"]]
        return result

    monkeypatch.setattr(nc, "_active_arp_probe", arp)
    result = nc.probe_cluster_physical_links(nodes, snapshots)
    assert result["ok"]
    assert result["status"] == "partial"  # 未接线口计入告警
    assert result["issues"] == []
    assert any("载波" in w and "carrier=0" in w for w in result["warnings"])
    # 二层探测只发生在已接线的 rail 上（双向 2 条）
    rail_iface = nc.ROCE_IFACES[0][0]
    assert len(result["links"]) == 2
    assert all(link["iface"] == rail_iface for link in result["links"])


def test_physical_probe_skips_arp_on_unplugged_rail(monkeypatch):
    """未接线的口不做二层探测，即使残留地址也不产生探测失败。"""
    nodes = [Node(id=1, name="n1", ip="192.0.2.1"), Node(id=2, name="n2", ip="192.0.2.2")]
    snapshots = _single_cable_snapshots(nodes)
    # 未接线口也残留旧地址；若被探测会故意失败，验证其已被跳过
    for node_id, mac in ((1, "00:00:00:00:01:ff"), (2, "00:00:00:00:02:ff")):
        for iface, rail in nc.ROCE_IFACES[1:]:
            snapshots[node_id][iface]["addresses"] = [f"192.168.{rail}.{node_id}"]

    def arp(_node, targets):
        rail_iface = nc.ROCE_IFACES[0][0]
        assert all(t["iface"] == rail_iface for t in targets), "不应探测未接线的 rail"
        result = {}
        for target in targets:
            target_id = int(target["key"].split(":")[1])
            result[target["key"]] = [snapshots[target_id][target["iface"]]["mac"]]
        return result

    monkeypatch.setattr(nc, "_active_arp_probe", arp)
    result = nc.probe_cluster_physical_links(nodes, snapshots)
    assert result["ok"] and len(result["links"]) == 2


def test_physical_probe_fails_when_node_has_no_link():
    """完全没有高速物理链路的节点仍应阻止建集群。"""
    node = Node(id=1, name="n1", ip="192.0.2.1")
    snapshots = {1: {
        iface: {"exists": True, "carrier": False, "operstate": "down",
                "mtu": 9000, "mac": f"00:00:00:00:01:{rail:02x}", "addresses": []}
        for iface, rail in nc.ROCE_IFACES
    }}
    result = nc.probe_cluster_physical_links([node], snapshots)
    assert not result["ok"]
    assert any("未检测到任何高速物理链路" in i for i in result["issues"])


def _fake_ssh(monkeypatch, plan, cable_rails, ping_ok):
    """打桩 ssh：指定已接线 rail，ping 按 ping_ok(target, cable_rails, ip) 判定。"""
    import time
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    ip = {idx: nc.node_ips(plan, idx) for idx in (1, 2)}

    def fake_exec(_client, cmd, **_kw):
        if "__FW_NOLINK__" in cmd:
            iface = next(i for i, _ in nc.ROCE_IFACES if i in cmd)
            if iface not in cable_rails:
                return ("0\n", "", 0)
            return (f"1\ninet {ip[1][iface]}/24\n", "", 0)
        if "gids" in cmd:
            return ("1\n", "", 0)
        if "ping" in cmd:
            target = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", cmd).group(1)
            ok = ping_ok(target, cable_rails, ip)
            return ("0% packet loss" if ok else "100% packet loss", "", 0)
        return ("", "", 0)

    class _FakeClient:
        def close(self):
            pass

    monkeypatch.setattr(nc.ssh_client, "connect", lambda *_a, **_k: _FakeClient())
    monkeypatch.setattr(nc.ssh_client, "exec", fake_exec)


def test_verify_node_network_single_cable_ok(monkeypatch):
    """只接一条高速网线的节点：已接线口校验通过、与对端单 rail 互通即为成功。"""
    plan = nc.plan_cluster_network("10.100.0.0/16", 9000)
    node, peer = Node(id=1, name="n1", ip="192.0.2.1"), Node(id=2, name="n2", ip="192.0.2.2")
    rail0 = nc.ROCE_IFACES[0][0]
    ips = {1: nc.node_ips(plan, 1), 2: nc.node_ips(plan, 2)}

    def ping_ok(target, connected, ip):
        return target == ip[2][rail0]

    _fake_ssh(monkeypatch, plan, {rail0}, ping_ok)
    ok, detail = nc.verify_node_network(node, plan, 1, [(peer, 2)])
    assert ok
    assert detail["connected_interfaces"] == [rail0]
    assert detail["local"] is True
    assert detail["ping"][rail0][f"n2@{ips[2][rail0]}"] is True
    # 未接线的口不参与 ping 判定
    assert all(detail["ping"][i] == {} for i in detail["ping"] if i != rail0)


def test_verify_node_network_fails_when_no_common_rail(monkeypatch):
    """双方接的不是同一条 rail 时无任何可达链路，验证必须失败。"""
    plan = nc.plan_cluster_network("10.100.0.0/16", 9000)
    node, peer = Node(id=1, name="n1", ip="192.0.2.1"), Node(id=2, name="n2", ip="192.0.2.2")
    rail0 = nc.ROCE_IFACES[0][0]

    def ping_ok(_target, _connected, _ip):
        return False  # 对端同 rail 不互通（如接错交换机端口）

    _fake_ssh(monkeypatch, plan, {rail0}, ping_ok)
    ok, detail = nc.verify_node_network(node, plan, 1, [(peer, 2)])
    assert not ok
    assert detail["ping"][rail0] != {}


def test_verify_peer_reachability_single_cable(monkeypatch):
    """既有成员到新节点：至少一条已接线 rail 可达即通过反向验证。"""
    plan = nc.plan_cluster_network("10.100.0.0/16", 9000)
    member, new_node = Node(id=1, name="n1", ip="192.0.2.1"), Node(id=2, name="n2", ip="192.0.2.2")
    rail0 = nc.ROCE_IFACES[0][0]
    ips = {1: nc.node_ips(plan, 1), 2: nc.node_ips(plan, 2)}

    def ping_ok(target, connected, ip):
        return target == ip[2][rail0]

    _fake_ssh(monkeypatch, plan, {rail0}, ping_ok)
    ok, detail = nc.verify_peer_reachability(member, plan, [(new_node, 2)])
    assert ok
    # 只对已接线口发起 ping，未接线口保持空结果
    assert detail[rail0][f"n2@{ips[2][rail0]}"] is True
    assert all(detail[i] == {} for i in detail if i != rail0)


def test_verify_peer_reachability_fails_on_unreachable_peer(monkeypatch):
    """既有成员只有一条 rail 且到新节点不通——文档级错误提示而非静默通过。"""
    plan = nc.plan_cluster_network("10.100.0.0/16", 9000)
    member, new_node = Node(id=1, name="n1", ip="192.0.2.1"), Node(id=2, name="n2", ip="192.0.2.2")
    rail0 = nc.ROCE_IFACES[0][0]

    _fake_ssh(monkeypatch, plan, {rail0}, lambda *_a: False)
    ok, detail = nc.verify_peer_reachability(member, plan, [(new_node, 2)])
    assert not ok
    assert detail[rail0][f"n2@{nc.node_ips(plan, 2)[rail0]}"] is False


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


# ---------- 创建集群始终按用户网段规划配置（不复用节点现网） ----------


def _configure_services_stubs(monkeypatch, nodes):
    """统一打桩：物理链路/ARP 全部通过，apply/verify 记录调用。"""
    from app.routers import clusters

    snapshots = {n.id: {} for n in nodes}
    monkeypatch.setattr(
        clusters.network_config_svc, "inspect_nodes_network", lambda _nodes: snapshots
    )
    monkeypatch.setattr(
        clusters.network_config_svc, "probe_cluster_physical_links",
        lambda _nodes, _snapshots=None: {
            "ok": True, "status": "verified", "issues": [], "links": [],
        },
    )
    monkeypatch.setattr(
        clusters.network_config_svc, "probe_plan_ip_conflicts",
        lambda _nodes, _plan, _indices, _snapshots=None: [],
    )
    monkeypatch.setattr(
        clusters.network_config_svc, "verify_node_network",
        lambda _node, _plan, _index, _peers: (True, {}),
    )
    applied = []
    monkeypatch.setattr(
        clusters.network_config_svc, "apply_node_network",
        lambda node, plan, index: applied.append((node.id, plan["cidr"], index)) or (True, "ok"),
    )
    return applied


def test_configure_network_uses_user_cidr(monkeypatch):
    """指定网段即按该网段规划配置（现网是否一致都不影响）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.models import Node
    from app.routers import clusters

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    n1, n2 = Node(id=1, name="n1", ip="192.0.2.1"), Node(id=2, name="n2", ip="192.0.2.2")
    db.add_all([n1, n2])
    db.commit()
    applied = _configure_services_stubs(monkeypatch, [n1, n2])

    plan, changed, indices = clusters._configure_cluster_network(db, [1, 2], "10.200.0.0/16", 9000)

    assert plan["cidr"] == "10.200.0.0/16"
    assert changed == [(n1, 1), (n2, 2)]
    assert applied == [(1, "10.200.0.0/16", 1), (2, "10.200.0.0/16", 2)]
    db.close()


def test_configure_network_auto_finds_cidr_when_unset(monkeypatch):
    """未提供网段时自动找首个空闲网段并配置。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.models import Node
    from app.routers import clusters

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    n1 = Node(id=1, name="n1", ip="192.0.2.1")
    db.add(n1)
    db.commit()
    applied = _configure_services_stubs(monkeypatch, [n1])

    plan, changed, indices = clusters._configure_cluster_network(db, [1], None, 9000)

    assert plan["cidr"] == "10.0.0.0/16"  # 首个可用
    assert applied == [(1, "10.0.0.0/16", 1)]
    db.close()
