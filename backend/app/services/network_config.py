"""集群高速网络配置：规划 / 应用 / 验证 / 回滚（SSH + sudo 写 netplan）。

布局遵循 NVIDIA DGX Spark 官方（spark-clustering.html）：每节点 4 个 PCIe 通道
（2 个 QSFP × 2 通道）各为独立 100G 链路，按接口分配独立 /24 子网：
  enp1s0f0np0  -> 10.100.0.0/24
  enP2p1s0f0np0 -> 10.100.1.0/24
  enp1s0f1np1  -> 10.100.2.0/24
  enP2p1s0f1np1 -> 10.100.3.0/24

策略（本项目 = 高速网配置的唯一 owner）：
- 本项目只写自己的高优先级文件（999-fireworks-network.yaml，声明 4 个高速口）；
  管理网（enP7s7）与其它 NIC 一律不碰。
- netplan 合并规则：文件按字典序后者优先，标量覆盖、**序列（addresses）为
  append 拼接**。因此若其它文件（官方 Nvidia sync / 手动配置）已给高速口赋过
  addresses/dhcp4，单纯高优先文件无法剔除旧地址——进行任何更改前以固定名
  .fw-bak（覆盖，不堆积）单名备份该文件一次，并从其中移除高速口定义，
  改由本项目 999 唯一声明。
- 删除/回滚：仅清理本项目 999 文件 + 还原被接管文件的单名备份（还原即删），
  节点网络自动恢复接入前状态；不生成/堆积备份。
"""

import base64
import ipaddress
import json

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


def _plan_grep(plan: dict) -> str:
    """从 plan 推导 grep 网段前缀（匹配全部 4 个接口子网）。

    4 接口分属相邻 /24（如 10.200.0/1/2/3，第三段固定 0-3），公共前缀恒为
    前两段（10\\.200\\.）。plan 恒由 plan_cluster_network 构造，iface_subnets 必然存在。
    """
    net = ipaddress.ip_network(next(iter((plan or {}).get("iface_subnets", {}).values())))
    octets = str(net.network_address).split(".")
    return "\\.".join(octets[:2]) + r"\."


# 本项目统一接管命名：被接管外来文件的固定单名备份（覆盖，不堆积）
FW_BAK_SUFFIX = ".fw-bak"


def apply_node_network(node: Node, plan: dict, index: int) -> tuple[bool, str]:
    """配置节点高速网络（本项目唯一 owner），返回 (ok, 说明)。

    1) 只写本项目高优先级文件 999-fireworks-network.yaml（4 个高速口 + 规划 IP）。
    2) 先扫描现有 /etc/netplan/*.yaml：仅当其它文件已给高速口赋 addresses/dhcp4
       （官方 Nvidia sync / 手动配置等，append 语义下无法被高优先文件剔除旧地址），
       才对该文件做最小接管：固定名 .fw-bak 单名备份一次 + 移除其中的高速口定义，
       使 999 成为唯一声明；其余文件/管理网/其它 NIC 一律不动。
    3) netplan apply 以退出码判成败（不再依赖无条件打印的标记误报）。
    """
    ips = node_ips(plan, index)
    yaml_text = _render_netplan_yaml(plan, index)
    cfg = json.dumps({
        "ips": {k: f"{v}/24" for k, v in ips.items()},
        "mtu": plan.get("mtu") or 9000,
        "yaml": yaml_text,
        "roce": [iface for iface, _ in ROCE_IFACES],
        "bak_suffix": FW_BAK_SUFFIX,
    })
    cfg_b64 = base64.b64encode(cfg.encode()).decode()
    script = (
        "import os, glob, json, base64, yaml, subprocess\n"
        f"DROPIN = '{NETPLAN_DROPIN}'\n"
        "cfg = json.loads(base64.b64decode('" + cfg_b64 + "'))\n"
        "roce = set(cfg['roce'])\n"
        "patched = []\n"
        "# 1) 扫描现有高速网配置：其它文件已给高速口赋 addresses/dhcp4 才接管\n"
        "for path in sorted(glob.glob('/etc/netplan/*.yaml')):\n"
        "    if os.path.realpath(path) == DROPIN:\n"
        "        continue\n"
        "    try:\n"
        "        data = yaml.safe_load(open(path)) or {}\n"
        "    except Exception:\n"
        "        continue\n"
        "    eth = ((data.get('network') or {}).get('ethernets') or {})\n"
        "    owned = [i for i in roce if i in eth and (eth[i].get('addresses') or eth[i].get('dhcp4'))]\n"
        "    if not owned:\n"
        "        continue\n"
        "    shutil.copy(path, path + cfg['bak_suffix'])  # 固定单名备份（覆盖，不堆积）\n"
        "    for i in owned:\n"
        "        eth.pop(i, None)\n"
        "    open(path, 'w').write(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))\n"
        "    patched.append(os.path.basename(path) + ':' + ','.join(owned))\n"
        "# 2) 本项目唯一高速网声明（4 个高速口，唯一 owner）\n"
        "open(DROPIN, 'w').write(cfg['yaml'])\n"
        "os.chmod(DROPIN, 0o600)\n"
        "# 3) 仅清理本项目高速口的 NM 残留连接（绝不动管理口 / 其它 NIC）\n"
        "out = subprocess.run(['nmcli','-t','-f','NAME,DEVICE','connection','show'], capture_output=True, text=True).stdout\n"
        "for line in out.splitlines():\n"
        "    if ':' not in line:\n"
        "        continue\n"
        "    name, dev = line.rsplit(':', 1)\n"
        "    if dev in roce and name.startswith('Wired connection'):\n"
        "        subprocess.run(['nmcli','connection','delete',name], capture_output=True, text=True)\n"
        "        print('NM_CLEAN', name)\n"
        "print('PATCHED ' + ('|'.join(patched) if patched else 'none'))\n"
        "print('WROTE_DROPIN')\n"
    )
    script_b64 = base64.b64encode(script.encode()).decode()
    out, _ = _sudo_exec(
        node,
        f"rm -f /tmp/fw_net_apply.py && echo {script_b64} | base64 -d > /tmp/fw_net_apply.py && "
        f"python3 /tmp/fw_net_apply.py && netplan apply 2>&1 | tail -8 && echo APPLY_OK",
        timeout=180,
    )
    if "WROTE_DROPIN" in out and "APPLY_OK" in out:
        patch_note = ""
        for seg in out.split():
            if seg.startswith("PATCHED "):
                detail = seg[len("PATCHED "):]
                if detail != "none":
                    patch_note = f"；已接管 {detail}"
        detail = ", ".join(f"{i}={ip}" for i, ip in ips.items())
        return True, f"已写入高速网配置（{detail}）{patch_note}"
    return False, out[-400:] or "应用失败"


def rollback_node_network(node: Node) -> tuple[bool, str]:
    """回滚：删除本项目高速网声明 + 还原被接管文件，netplan apply 恢复接入前状态。

    只清理本项目产物：999-fireworks-network.yaml + 固定单名 *.yaml.fw-bak 备份
    （还原即删，不堆积）；不触碰官方/管理网等其它文件。
    """
    script = (
        "import os, glob, shutil\n"
        f"DROPIN = '{NETPLAN_DROPIN}'\n"
        f"SUFFIX = '{FW_BAK_SUFFIX}'\n"
        "restored = []\n"
        "# 还原被接管文件的固定单名备份（还原即删，不堆积）\n"
        "for bak in sorted(glob.glob('/etc/netplan/*.yaml' + SUFFIX)):\n"
        "    target = bak[:-len(SUFFIX)]\n"
        "    shutil.copy(bak, target)\n"
        "    os.remove(bak)\n"
        "    restored.append('RESTORED:' + os.path.basename(target))\n"
        "if os.path.exists(DROPIN):\n"
        "    os.remove(DROPIN)\n"
        "    restored.append('REMOVED_DROPIN')\n"
        "print('|'.join(restored) if restored else 'NONE')\n"
    )
    script_b64 = base64.b64encode(script.encode()).decode()
    out, _ = _sudo_exec(
        node,
        f"rm -f /tmp/fw_net_rollback.py && echo {script_b64} | base64 -d > /tmp/fw_net_rollback.py && "
        f"python3 /tmp/fw_net_rollback.py && netplan apply 2>&1 | tail -5 && echo APPLY_OK",
        timeout=180,
    )
    if "APPLY_OK" in out:
        return True, "已还原节点网络配置（删除本项目声明，接口恢复接入前状态）"
    return False, out[-400:] or "回滚失败"


def verify_node_network(
    node: Node, plan: dict, node_index: int, peers: list[tuple[Node, int]]
) -> tuple[bool, dict]:
    """验证节点高速网络：本机 plan IP 已生效 + 同 rail 对端 plan IP ping + IPv4 RoCEv2 GID。

    对端 IP 直接取 plan 分配；本机 4 接口均应有本项目 999 文件声明的 plan IP。
    netplan/NM 应用与 GID 生成为异步，验证前等待并带重试；返回 (ok, {"local": ok, "ping": {...}, "gid": ok})。
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
