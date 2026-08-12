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
