"""Phase4+ 网络规划纯函数回归：plan / node_ips / _plan_grep / 渲染。"""

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
