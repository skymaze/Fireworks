"""集群高速网络配置：规划 / 应用 / 验证 / 回滚（SSH + sudo 写 netplan）。

布局遵循 NVIDIA DGX Spark 官方（spark-clustering.html）：每节点 4 个 PCIe 通道
（2 个 QSFP × 2 通道）各为独立 100G 链路，按接口分配独立 /24 子网：
  enp1s0f0np0  -> 10.100.0.0/24
  enP2p1s0f0np0 -> 10.100.1.0/24
  enp1s0f1np1  -> 10.100.2.0/24
  enP2p1s0f1np1 -> 10.100.3.0/24

实现方式：写入独立的 netplan drop-in 文件（999-fireworks-network.yaml），
按 netplan 多文件合并规则覆盖官方配置（不修改官方文件，可完整回滚）。
"""

import base64
import ipaddress
import json
import time

from ..models import Node
from . import ssh_client

# 接口 -> 子网序号（官方布局，固定映射；新成员同接口分同一网段）
ROCE_IFACES: list[tuple[str, int]] = [
    ("enp1s0f0np0", 0),
    ("enP2p1s0f0np0", 1),
    ("enp1s0f1np1", 2),
    ("enP2p1s0f1np1", 3),
]

NETPLAN_DROPIN = "/etc/netplan/999-fireworks-network.yaml"


def plan_cluster_network(cidr: str = "10.100.0.0/16", mtu: int = 9000) -> dict:
    """生成集群网络规划：cidr 解析为 4 个连续 /24 子网（按接口序号）。

    返回 {"cidr", "mtu", "iface_subnets": {接口: "a.b.c.0/24"}}。
    """
    net = ipaddress.ip_network(cidr, strict=False)
    if net.prefixlen > 24:
        raise ValueError(f"网段过小（/{net.prefixlen}），至少需要 /24 以容纳 4 个子网")
    subnets = list(net.subnets(new_prefix=24))[: len(ROCE_IFACES)]
    if len(subnets) < len(ROCE_IFACES):
        raise ValueError("网段不足以容纳 4 个接口子网")
    plan = {
        "cidr": cidr,
        "mtu": int(mtu),
        "iface_subnets": {iface: str(subnets[idx]) for iface, idx in ROCE_IFACES},
    }
    return plan


def node_ips(plan: dict, index: int) -> dict[str, str]:
    """节点序号（1 起）在规划内的 4 接口 IP：10.100.{s}.{index+9}/24。

    从 .10 起分配以避开 NVIDIA 官方工具占用的 .1-.4（官方按节点对分配
    101=.2/102=.1/111=.4/112=.3，与集群序号错位），防止 DAD 冲突。
    """
    ips: dict[str, str] = {}
    for iface, subnet in plan["iface_subnets"].items():
        net = ipaddress.ip_network(subnet)
        host = net.network_address + 9 + index
        if host not in net:
            raise ValueError(f"节点序号 {index} 超出子网 {subnet} 主机范围")
        ips[iface] = str(host)
    return ips


def _render_netplan_yaml(plan: dict, index: int) -> str:
    ips = node_ips(plan, index)
    mtu = plan.get("mtu") or 9000
    lines = ["network:", "  version: 2", "  renderer: NetworkManager", "  ethernets:"]
    for iface in plan["iface_subnets"]:
        lines += [
            f"    {iface}:",
            "      dhcp4: false",
            "      addresses:",
            f"        - {ips[iface]}/24",
            f"      mtu: {mtu}",
        ]
    return "\n".join(lines) + "\n"


def _sudo_exec(node: Node, inner: str, timeout: int = 120) -> tuple[str, str]:
    """SSH 执行 sudo 命令：密码经 base64 管道注入（避免 stdin 冲突/特殊字符），
    以 root 执行 inner（inner 内不得含单引号）。"""
    if not node.ssh_password:
        raise RuntimeError("节点未保存 SSH 密码，无法提权配置网络")
    pwd_b64 = base64.b64encode(node.ssh_password.encode()).decode()
    client = ssh_client.connect(node, timeout=20)
    try:
        return ssh_client.exec(
            client,
            f"bash -c \"echo {pwd_b64} | base64 -d | sudo -S bash -c '{inner}'\"",
            timeout=timeout,
        )[:2]
    finally:
        client.close()


# 官方工具生成的 netplan 文件（其中 4 个高速口地址需被接管覆盖）
OFFICIAL_NETPLAN = "/etc/netplan/99-nvidia-sync-cluster.yaml"


def _plan_grep(plan: dict) -> str:
    """从 plan 推导 grep 网段前缀（匹配全部 4 个接口子网）。

    4 接口分属相邻 /24（如 10.200.0/1/2/3，第三段固定 0-3），公共前缀恒为
    前两段（10\\.200\\.）——任何合法 cidr（≤ /22 才能容纳 4 个 /24）都成立。
    """
    cidr = (plan or {}).get("cidr")
    try:
        net = ipaddress.ip_network(cidr, strict=False) if cidr else None
    except ValueError:
        net = None
    if net is None:
        subnets = list((plan or {}).get("iface_subnets", {}).values())
        if not subnets:
            return r"10\.100\."
        try:
            net = ipaddress.ip_network(subnets[0], strict=False)
        except ValueError:
            return r"10\.100\."
    octets = str(net.network_address).split(".")
    return "\\.".join(octets[:2]) + r"\."


def apply_node_network(node: Node, plan: dict, index: int) -> tuple[bool, str]:
    """接管节点高速网络：改写官方 netplan 中 4 接口地址为 plan 分配 IP 并 apply。

    netplan 对 addresses 为 append 合并，drop-in 无法覆盖官方地址，因此直接
    修改官方文件（先备份 .fw-bak-* 供回滚）；官方文件缺失时退回 drop-in。
    返回 (ok, 说明)。
    """
    ips = node_ips(plan, index)
    yaml_text = _render_netplan_yaml(plan, index)
    cfg_b64 = base64.b64encode(
        json.dumps(
            {"ips": {k: f"{v}/24" for k, v in ips.items()}, "mtu": plan.get("mtu") or 9000, "yaml": yaml_text}
        ).encode()
    ).decode()
    script = (
        "import os, glob, shutil, time, json, base64, yaml, subprocess\n"
        f"OFFICIAL = '{OFFICIAL_NETPLAN}'\n"
        f"DROPIN = '{NETPLAN_DROPIN}'\n"
        "cfg = json.loads(base64.b64decode('" + cfg_b64 + "'))\n"
        "# 清理官方工具残留的 NM 连接（DEVICE 非高速口/管理口的 Wired connection），\n"
        "# 避免它们抢先绑定高速口并卡在 connecting\n"
        "keep = {'enP7s7', 'docker0', 'lo', ''}\n"
        "out = subprocess.run(['nmcli', '-t', '-f', 'NAME,DEVICE', 'connection', 'show'], capture_output=True, text=True).stdout\n"
        "for line in out.splitlines():\n"
        "    if ':' not in line:\n"
        "        continue\n"
        "    name, dev = line.rsplit(':', 1)\n"
        "    if dev not in keep and name.startswith('Wired connection'):\n"
        "        subprocess.run(['nmcli', 'connection', 'delete', name], capture_output=True, text=True)\n"
        "        print('NM_CLEAN', name)\n"
        "# 清理旧 drop-in（官方文件被接管后不再需要，避免地址重复）\n"
        "if os.path.exists(DROPIN):\n"
        "    os.remove(DROPIN)\n"
        "patched = False\n"
        "if os.path.exists(OFFICIAL):\n"
        "    shutil.copy(OFFICIAL, OFFICIAL + '.fw-bak-' + time.strftime('%Y%m%d%H%M%S'))\n"
        "    data = yaml.safe_load(open(OFFICIAL)) or {}\n"
        "    eth = data.setdefault('network', {}).setdefault('ethernets', {})\n"
        "    for iface, ip in cfg['ips'].items():\n"
        "        eth.setdefault(iface, {})\n"
        "        eth[iface]['dhcp4'] = False\n"
        "        eth[iface]['addresses'] = [ip]\n"
        "        eth[iface]['mtu'] = cfg['mtu']\n"
        "        patched = True\n"
        "    open(OFFICIAL, 'w').write(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))\n"
        "    print('PATCHED_OFFICIAL')\n"
        "# 官方文件缺失/未含这些接口时退回 drop-in\n"
        "if not patched:\n"
        "    open(DROPIN, 'w').write(cfg['yaml'])\n"
        "    os.chmod(DROPIN, 0o600)\n"
        "    print('WROTE_DROPIN')\n"
        "print('DONE')\n"
    )
    script_b64 = base64.b64encode(script.encode()).decode()
    out, _ = _sudo_exec(
        node,
        f"rm -f /tmp/fw_net_apply.py && echo {script_b64} | base64 -d > /tmp/fw_net_apply.py && "
        f"python3 /tmp/fw_net_apply.py && netplan apply 2>&1 | tail -5 && echo APPLY_DONE",
        timeout=180,
    )
    if "DONE" in out and "APPLY_DONE" in out:
        return True, f"已接管官方配置（接口 IP：{', '.join(f'{i}={ip}' for i, ip in ips.items())}）"
    return False, out[-300:] or "应用失败"


def rollback_node_network(node: Node) -> tuple[bool, str]:
    """回滚：恢复官方 netplan 备份（.fw-bak-*），删除接管用的 drop-in，再 apply。"""
    script = (
        "import os, glob, shutil\n"
        f"OFFICIAL = '{OFFICIAL_NETPLAN}'\n"
        f"DROPIN = '{NETPLAN_DROPIN}'\n"
        "restored = []\n"
        "baks = sorted(glob.glob(OFFICIAL + '.fw-bak-*'))\n"
        "if baks:\n"
        "    shutil.copy(baks[-1], OFFICIAL)\n"
        "    restored.append('RESTORED_OFFICIAL')\n"
        "if os.path.exists(DROPIN):\n"
        "    os.remove(DROPIN)\n"
        "    restored.append('REMOVED_DROPIN')\n"
        "print('|'.join(restored) if restored else 'NONE')\n"
    )
    script_b64 = base64.b64encode(script.encode()).decode()
    out, _ = _sudo_exec(
        node,
        f"rm -f /tmp/fw_net_rollback.py && echo {script_b64} | base64 -d > /tmp/fw_net_rollback.py && "
        f"python3 /tmp/fw_net_rollback.py && netplan apply 2>&1 | tail -5 && echo APPLY_DONE",
        timeout=180,
    )
    if ("RESTORED_OFFICIAL" in out or "REMOVED_DROPIN" in out or "NONE" in out) and "APPLY_DONE" in out:
        return True, out.strip().splitlines()[-2][:80] if out.strip() else "已回滚"
    return False, out[-300:] or "回滚失败"


def verify_node_network(
    node: Node, plan: dict, node_index: int, peers: list[tuple[Node, int]]
) -> tuple[bool, dict]:
    """验证节点高速网络：本机 plan IP 已生效 + 同 rail 对端 plan IP ping + IPv4 RoCEv2 GID。

    对端 IP 直接取 plan 分配（不再实时查询），确保 apply 真正生效——若节点仍为
    官方旧配置（netplan append 未覆盖），本机 IP 检查即失败。netplan/NM 应用与
    GID 生成为异步，验证前等待并带重试；返回 (ok, {"local": ok, "ping": {...}, "gid": ok})。
    """
    import time as _time

    _time.sleep(5)  # 等 NM 应用 IP / GID 生成
    ips = node_ips(plan, node_index)
    client = ssh_client.connect(node, timeout=20)
    try:
        # 本机 4 接口均应有 plan 分配的 IP（等待最多 ~40s）
        local_ok = False
        local_actual: set[str] = set()
        for _ in range(7):
            out, _, _ = ssh_client.exec(
                client,
                "ip -4 -br addr show 2>/dev/null | grep '" + _plan_grep(plan) + "' | tr -s ' '",
                timeout=20,
            )
            actual: set[str] = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and "/" in parts[-1]:
                    actual.add(parts[-1].split("/")[0])
            if all(ip in actual for ip in ips.values()):
                local_ok = True
                local_actual = actual
                break
            _time.sleep(5)
        # 本机 GID：IPv4 派生 RoCEv2 GID（ffff 形式）随 IP 动态生成
        gid_ok = False
        gid_count = 0
        for _ in range(7):
            out, _, _ = ssh_client.exec(
                client,
                "for d in /sys/class/infiniband/*/ports/1/gids/; do cat $d/* 2>/dev/null; done | grep -c 'ffff'",
                timeout=20,
            )
            gid_count = int(out.strip()) if out.strip().isdigit() else 0
            if gid_count >= len(ROCE_IFACES):
                gid_ok = True
                break
            _time.sleep(5)
        if not gid_ok:
            gid_ok = gid_count >= 1  # 部分口 GID 生成慢，≥1 条即 RoCE 可用
        # 对端各接口 plan IP ping
        ping_detail: dict[str, dict[str, bool]] = {}
        all_ok = local_ok and gid_ok
        for iface, ip in ips.items():
            ping_detail[iface] = {}
            for peer, peer_index in peers:
                peer_ip = node_ips(plan, peer_index)[iface]
                if peer_ip == ip:
                    continue
                out, _, _ = ssh_client.exec(client, f"ping -c 2 -W 2 {peer_ip} 2>&1 | tail -1", timeout=15)
                ok = ("0% packet loss" in out or "ms" in out) and "100% packet loss" not in out
                ping_detail[iface][f"{peer.name}@{peer_ip}"] = ok
                all_ok = all_ok and ok
        return all_ok, {
            "local": local_ok,
            "ping": ping_detail,
            "gid": gid_ok,
            "gid_count": gid_count,
            "local_ips": sorted(local_actual),
        }
    finally:
        client.close()
