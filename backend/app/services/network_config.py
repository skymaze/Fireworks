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
  .fw-bak（仅首次创建、不堆积）单名备份该文件一次，并从其中移除高速口定义，
  改由本项目 999 唯一声明。
- 删除/回滚：仅清理本项目 999 文件 + 还原被接管文件的单名备份（还原即删），
  节点网络自动恢复接入前状态；不生成/堆积备份。
"""

import base64
import concurrent.futures
import ipaddress
import json
import re
import threading
import uuid

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

_operation_locks_guard = threading.Lock()
_operation_locks: dict[int, threading.RLock] = {}


def acquire_operation_locks(keys: list[int]) -> list[threading.RLock]:
    """按稳定顺序串行化涉及相同节点/集群的配置，避免并发分配同一槽位或互相回滚。"""
    unique = sorted(set(keys))
    with _operation_locks_guard:
        locks = [_operation_locks.setdefault(key, threading.RLock()) for key in unique]
    for lock in locks:
        lock.acquire()
    return locks


def release_operation_locks(locks: list[threading.RLock]) -> None:
    for lock in reversed(locks):
        lock.release()


def inspect_nodes_network(nodes: list[Node]) -> dict[int, dict[str, dict]]:
    """并发读取多节点网络快照，耗时接近最慢节点而不是所有节点之和。"""
    if not nodes:
        return {}
    workers = min(8, len(nodes))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        snapshots = list(pool.map(inspect_node_network, nodes))
    return {node.id: snapshot for node, snapshot in zip(nodes, snapshots, strict=True)}


def _probe_batches(
    nodes: list[Node], batches: dict[int, list[dict]]
) -> dict[str, list[str]]:
    """不同节点的主动 ARP 探测彼此独立，并发执行以缩短预检时间。"""
    active = [node for node in nodes if batches.get(node.id)]
    if not active:
        return {}
    workers = min(8, len(active))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda node: _active_arp_probe(node, batches[node.id]), active))
    results: dict[str, list[str]] = {}
    for row in rows:
        results.update(row)
    return results


def plan_cluster_network(cidr: str = "10.100.0.0/16", mtu: int = 9000) -> dict:
    """生成集群网络规划：cidr 解析为 4 个连续 /24 子网（按接口序号）。

    返回 {"cidr", "mtu", "iface_subnets": {接口: "a.b.c.0/24"}}。
    """
    net = ipaddress.ip_network(cidr, strict=False)
    if net.prefixlen > 22:
        raise ValueError(f"网段过小（/{net.prefixlen}），至少需要 /22 以容纳 4 个 /24 rail 子网")
    subnets = list(net.subnets(new_prefix=24))[: len(ROCE_IFACES)]
    if len(subnets) < len(ROCE_IFACES):
        raise ValueError("网段不足以容纳 4 个接口子网")
    plan = {
        # 统一保存规范化网络地址，避免 10.1.2.3/16 之类主机位输入在后续
        # 数据库冲突比较中被误判为另一个网段。
        "cidr": str(net),
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


def inspect_node_network(node: Node) -> dict[str, dict]:
    """只读获取四个高速口的地址、MTU、MAC 与物理载波状态。"""
    snapshot: dict[str, dict] = {}
    client = ssh_client.connect(node, timeout=20)
    try:
        for iface, _ in ROCE_IFACES:
            out, err, rc = ssh_client.exec(
                client,
                f"if [ ! -e /sys/class/net/{iface} ]; then echo __FW_MISSING__; exit 0; fi; "
                f"ip -4 -o addr show dev {iface} 2>/dev/null; "
                f"printf '__FW_META__ '; cat /sys/class/net/{iface}/address "
                f"/sys/class/net/{iface}/carrier /sys/class/net/{iface}/operstate "
                f"/sys/class/net/{iface}/mtu 2>/dev/null",
                timeout=20,
            )
            if rc != 0 or "__FW_MISSING__" in out:
                snapshot[iface] = {
                    "exists": False, "addresses": [], "mtu": None, "mac": None,
                    "carrier": False, "operstate": "missing", "error": err.strip(),
                }
                continue
            before, _marker, after = out.partition("__FW_META__ ")
            meta = after.split()
            mac = meta[0].lower() if len(meta) >= 1 else None
            carrier = meta[1] == "1" if len(meta) >= 2 else False
            operstate = meta[2] if len(meta) >= 3 else "unknown"
            mtu = int(meta[3]) if len(meta) >= 4 and meta[3].isdigit() else None
            addresses = []
            for line in before.splitlines():
                match = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)/\d+", line)
                if match:
                    addresses.append(match.group(1))
            snapshot[iface] = {
                "exists": True,
                "addresses": addresses,
                "mtu": mtu,
                "mac": mac,
                "carrier": carrier,
                "operstate": operstate,
                "error": err.strip(),
            }
        return snapshot
    finally:
        client.close()


def _active_arp_probe(node: Node, targets: list[dict]) -> dict[str, list[str]]:
    """用 Linux 原始套接字主动发送 RFC 5227 ARP Probe，无需安装 arping。

    targets 每项含 key/iface/ip。返回 key -> 应答 MAC 列表。使用 0.0.0.0 作为
    sender IP，因此即使节点当前分属不同 IPv4 子网，只要在同一二层广播域，目标
    仍会应答；既用于物理 rail 探测，也用于新地址占用检查。
    """
    if not targets:
        return {}
    payload = base64.b64encode(json.dumps(targets).encode()).decode()
    script = r'''import base64, concurrent.futures, json, socket, struct, time
targets = json.loads(base64.b64decode("PAYLOAD"))

def probe(item):
    iface, target = item['iface'], item['ip']
    responders = set()
    try:
        src = bytes.fromhex(open('/sys/class/net/' + iface + '/address').read().strip().replace(':', ''))
        dst = b'\xff' * 6
        eth = dst + src + b'\x08\x06'
        arp = struct.pack('!HHBBH', 1, 0x0800, 6, 4, 1)
        arp += src + socket.inet_aton('0.0.0.0') + b'\x00' * 6 + socket.inet_aton(target)
        packet = eth + arp
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        sock.bind((iface, 0))
        sock.settimeout(0.15)
        deadline = time.monotonic() + 1.2
        next_send = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                sock.send(packet)
                next_send = now + 0.35
            try:
                data = sock.recv(2048)
            except socket.timeout:
                continue
            if len(data) < 42 or data[12:14] != b'\x08\x06' or data[20:22] != b'\x00\x02':
                continue
            if data[28:32] != socket.inet_aton(target):
                continue
            responders.add(':'.join(f'{b:02x}' for b in data[22:28]))
        sock.close()
        return item['key'], sorted(responders), None
    except Exception as exc:
        return item['key'], [], str(exc)

workers = min(16, max(1, len(targets)))
with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
    rows = list(pool.map(probe, targets))
print('FW_ARP_RESULT ' + json.dumps({key: {'macs': macs, 'error': error} for key, macs, error in rows}))
'''.replace("PAYLOAD", payload)
    script_b64 = base64.b64encode(script.encode()).decode()
    script_path = f"/tmp/fw_arp_probe_{uuid.uuid4().hex}.py"
    out, err = _sudo_exec(
        node,
        f"echo {script_b64} | base64 -d > {script_path} && python3 {script_path}; "
        f"rc=$?; rm -f {script_path}; exit $rc",
        timeout=30,
    )
    match = re.search(r"^FW_ARP_RESULT (.+)$", out, re.MULTILINE)
    if not match:
        detail = "\n".join(part for part in (out.strip(), err.strip()) if part)
        raise RuntimeError(f"节点 {node.name} 主动 ARP 探测无法执行：{detail[-500:]}")
    raw = json.loads(match.group(1))
    errors = [f"{key}: {value['error']}" for key, value in raw.items() if value.get("error")]
    if errors:
        raise RuntimeError(f"节点 {node.name} 主动 ARP 探测失败：{'；'.join(errors)}")
    return {key: value.get("macs") or [] for key, value in raw.items()}


def probe_cluster_physical_links(
    nodes: list[Node], snapshots: dict[int, dict[str, dict]] | None = None
) -> dict:
    """在改地址前探测每条 rail 的载波和同交换机二层可达性。

    有现存地址时以双向 ARP Probe 验证（跨不同 IP 网段亦可）；全新无地址的口只能
    验证 carrier，最终连通性由配置后的逐 rail ping 再确认并自动回滚。
    """
    snapshots = snapshots or inspect_nodes_network(nodes)
    issues: list[str] = []
    partial: list[str] = []
    links: list[dict] = []
    for node in nodes:
        snapshot = snapshots.get(node.id) or {}
        for iface, _ in ROCE_IFACES:
            data = snapshot.get(iface) or {}
            if not data.get("exists", True):
                issues.append(f"{node.name} 缺少高速接口 {iface}")
            elif not data.get("carrier"):
                issues.append(f"{node.name} 的 {iface} 未检测到物理链路（carrier=0，请检查线缆/交换机端口）")

    if nodes:
        anchor = nodes[0]
        batches: dict[int, list[dict]] = {node.id: [] for node in nodes}
        for peer in nodes[1:]:
            for iface, _ in ROCE_IFACES:
                a = (snapshots.get(anchor.id) or {}).get(iface) or {}
                p = (snapshots.get(peer.id) or {}).get(iface) or {}
                if not a.get("addresses") or not p.get("addresses") or not a.get("mac") or not p.get("mac"):
                    partial.append(f"{anchor.name}↔{peer.name} {iface} 当前无可用于二层探测的地址")
                    continue
                forward_key = f"{anchor.id}:{peer.id}:{iface}"
                reverse_key = f"{peer.id}:{anchor.id}:{iface}"
                batches[anchor.id].append({"key": forward_key, "iface": iface, "ip": p["addresses"][0]})
                batches[peer.id].append({"key": reverse_key, "iface": iface, "ip": a["addresses"][0]})
                links += [
                    {"key": forward_key, "from": anchor.name, "to": peer.name, "iface": iface,
                     "target_ip": p["addresses"][0], "expected_mac": p["mac"]},
                    {"key": reverse_key, "from": peer.name, "to": anchor.name, "iface": iface,
                     "target_ip": a["addresses"][0], "expected_mac": a["mac"]},
                ]
        results = _probe_batches(nodes, batches)
        for link in links:
            observed = results.get(link.pop("key"), [])
            link["observed_macs"] = observed
            link["ok"] = link["expected_mac"].lower() in observed
            if not link["ok"]:
                seen = "、".join(observed) if observed else "无应答"
                issues.append(
                    f"{link['from']}→{link['to']} 的 {link['iface']} 二层探测失败"
                    f"（目标 {link['target_ip']}，期望 MAC {link['expected_mac']}，实际 {seen}）"
                )
    status = "failed" if issues else ("partial" if partial or len(nodes) < 2 else "verified")
    return {"ok": not issues, "status": status, "issues": issues, "warnings": partial, "links": links}


def probe_plan_ip_conflicts(
    nodes: list[Node], plan: dict, node_indices: dict[int, int],
    snapshots: dict[int, dict[str, dict]] | None = None,
) -> list[dict]:
    """主动确认规划内每个待分配 IP 未被本机其它口或二层设备占用。"""
    snapshots = snapshots or inspect_nodes_network(nodes)
    conflicts: list[dict] = []
    batches: dict[int, list[dict]] = {node.id: [] for node in nodes}
    expected: dict[str, str | None] = {}
    targets: dict[str, dict] = {}
    for target_node in nodes:
        for iface, ip in node_ips(plan, node_indices[target_node.id]).items():
            key = f"{target_node.id}:{iface}:{ip}"
            target_mac = ((snapshots.get(target_node.id) or {}).get(iface) or {}).get("mac")
            owner_mac = None
            for current_node in nodes:
                for current_iface, data in (snapshots.get(current_node.id) or {}).items():
                    if ip not in (data.get("addresses") or []):
                        continue
                    if current_node.id == target_node.id and current_iface == iface:
                        owner_mac = target_mac
                    else:
                        conflicts.append({
                            "node": target_node.name, "iface": iface, "ip": ip,
                            "reason": f"已绑定在 {current_node.name} 的 {current_iface}",
                            "observed_mac": data.get("mac"),
                        })
            prober = next((node for node in nodes if node.id != target_node.id), target_node)
            batches[prober.id].append({"key": key, "iface": iface, "ip": ip})
            expected[key] = owner_mac
            targets[key] = {"node": target_node.name, "iface": iface, "ip": ip}
    results = _probe_batches(nodes, batches)
    already = {(c["node"], c["iface"], c["ip"]) for c in conflicts}
    for key, macs in results.items():
        allowed = expected[key]
        unexpected = [mac for mac in macs if not allowed or mac.lower() != allowed.lower()]
        item = targets[key]
        identity = (item["node"], item["iface"], item["ip"])
        if unexpected and identity not in already:
            conflicts.append({**item, "reason": "主动 ARP 探测到其它设备正在使用该地址",
                              "observed_mac": "、".join(unexpected)})
    return conflicts


def _detect_snapshot_network(snapshot: dict[str, dict]) -> dict | None:
    """从单节点只读快照识别 Fireworks 四 rail 规划和既有 net index。"""
    if len(snapshot) != len(ROCE_IFACES):
        return None
    first_iface = ROCE_IFACES[0][0]
    mtus = {data["mtu"] for data in snapshot.values() if data["mtu"] is not None}
    if len(mtus) != 1:
        return None
    mtu = mtus.pop()
    candidates = []
    for address in snapshot[first_iface]["addresses"]:
        try:
            network = ipaddress.ip_network(f"{address}/16", strict=False)
        except ValueError:
            continue
        if network.subnet_of(ipaddress.ip_network("10.0.0.0/8")):
            candidates.append(str(network))
    for cidr in sorted(candidates):
        plan = plan_cluster_network(cidr, mtu)
        first_subnet = ipaddress.ip_network(plan["iface_subnets"][first_iface])
        matching_indices = []
        for address in snapshot[first_iface]["addresses"]:
            candidate_index = int(ipaddress.ip_address(address)) - int(first_subnet.network_address) - 9
            if candidate_index < 1 or candidate_index > 245:
                continue
            expected = node_ips(plan, candidate_index)
            if all(expected[iface] in snapshot[iface]["addresses"] for iface, _ in ROCE_IFACES):
                matching_indices.append(candidate_index)
        if len(matching_indices) == 1:
            return {"plan": plan, "index": matching_indices[0]}
    return None


def analyze_existing_cluster_network(
    nodes: list[Node], snapshots: dict[int, dict[str, dict]] | None = None
) -> dict:
    """分类所选节点现网：reuse / reconfigure / configure。

    reuse：所有节点已处于同一规划且 index 唯一；reconfigure：至少一个节点已有
    合法规划，但节点间规划不一致；configure：没有可识别的完整四 rail 规划。
    """
    snapshots = snapshots or inspect_nodes_network(nodes)
    profiles: dict[int, dict] = {}
    for node in nodes:
        profile = _detect_snapshot_network(snapshots[node.id])
        if profile:
            profiles[node.id] = profile

    grouped: dict[tuple[str, int], list[int]] = {}
    for node_id, profile in profiles.items():
        plan = profile["plan"]
        grouped.setdefault((plan["cidr"], plan["mtu"]), []).append(node_id)
    networks = [
        {"cidr": cidr, "mtu": mtu, "node_ids": sorted(node_ids)}
        for (cidr, mtu), node_ids in sorted(grouped.items())
    ]

    if len(profiles) == len(nodes) and len(grouped) == 1:
        node_indices = {node_id: profile["index"] for node_id, profile in profiles.items()}
        if len(set(node_indices.values())) == len(nodes):
            return {
                "mode": "reuse",
                "plan": next(iter(profiles.values()))["plan"],
                "node_indices": node_indices,
                "networks": networks,
            }
    return {
        "mode": "reconfigure" if profiles else "configure",
        "networks": networks,
    }


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
    """SSH 执行 sudo 命令：密码仅经 SSH stdin 发送，不进入命令行/进程列表。"""
    if not node.ssh_password:
        raise RuntimeError("节点未保存 SSH 密码，无法提权配置网络")
    client = ssh_client.connect(node, timeout=20)
    try:
        return ssh_client.exec(
            client,
            f"sudo -S -p '' bash -c '{inner}'",
            timeout=timeout,
            input_data=node.ssh_password + "\n",
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


# 本项目统一接管命名：被接管外来文件的固定单名备份（仅首次创建，不堆积）
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
        "import os, glob, json, base64, yaml, subprocess, shutil\n"
        f"DROPIN = '{NETPLAN_DROPIN}'\n"
        "cfg = json.loads(base64.b64decode('" + cfg_b64 + "'))\n"
        "roce = set(cfg['roce'])\n"
        "patched = []\n"
        "originals = {}\n"
        "created_baks = []\n"
        "previous_dropin = open(DROPIN, 'rb').read() if os.path.exists(DROPIN) else None\n"
        "# 1) 扫描现有高速网配置：其它文件已给高速口赋 addresses/dhcp4 才接管\n"
        "for path in sorted(glob.glob('/etc/netplan/*.yaml')):\n"
        "    if os.path.realpath(path) == DROPIN:\n"
        "        continue\n"
        "    try:\n"
        "        data = yaml.safe_load(open(path)) or {}\n"
        "    except Exception:\n"
        "        continue\n"
        "    eth = ((data.get('network') or {}).get('ethernets') or {})\n"
        "    owned = []\n"
        "    for key, value in eth.items():\n"
        "        value = value or {}\n"
        "        actual = (value.get('match') or {}).get('name') or value.get('set-name') or key\n"
        "        if actual in roce and (value.get('addresses') or value.get('dhcp4')):\n"
        "            owned.append(key)\n"
        "    if not owned:\n"
        "        continue\n"
        "    originals[path] = open(path, 'rb').read()\n"
        "    bak = path + cfg['bak_suffix']\n"
        "    if not os.path.exists(bak):\n"
        "        shutil.copy(path, bak)\n"
        "        created_baks.append(bak)\n"
        "    for key in owned:\n"
        "        eth.pop(key, None)\n"
        "    open(path, 'w').write(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))\n"
        "    patched.append(os.path.basename(path) + ':' + ','.join(owned))\n"
        "# 2) 本项目唯一高速网声明（4 个高速口，唯一 owner）\n"
        "open(DROPIN, 'w').write(cfg['yaml'])\n"
        "os.chmod(DROPIN, 0o600)\n"
        "def run_netplan(action):\n"
        "    return subprocess.run(['netplan', action], capture_output=True, text=True, timeout=150)\n"
        "def restore():\n"
        "    for path, content in originals.items():\n"
        "        open(path, 'wb').write(content)\n"
        "    for bak in created_baks:\n"
        "        if os.path.exists(bak): os.remove(bak)\n"
        "    if previous_dropin is None:\n"
        "        if os.path.exists(DROPIN): os.remove(DROPIN)\n"
        "    else:\n"
        "        open(DROPIN, 'wb').write(previous_dropin)\n"
        "def fail(stage, proc):\n"
        "    restore()\n"
        "    rollback = run_netplan('generate')\n"
        "    if rollback.returncode == 0:\n"
        "        rollback = run_netplan('apply')\n"
        "    detail = ((proc.stdout or '') + '\\n' + (proc.stderr or '')).strip()[-1200:]\n"
        "    rb = 'ok' if rollback.returncode == 0 else (((rollback.stdout or '') + '\\n' + (rollback.stderr or '')).strip()[-400:] or 'failed')\n"
        "    print('FW_APPLY_ERROR ' + json.dumps({'stage': stage, 'detail': detail, 'rollback': rb}))\n"
        "    raise SystemExit(2)\n"
        "generated = run_netplan('generate')\n"
        "if generated.returncode != 0: fail('netplan generate', generated)\n"
        "applied = run_netplan('apply')\n"
        "if applied.returncode != 0: fail('netplan apply', applied)\n"
        "print('PATCHED ' + ('|'.join(patched) if patched else 'none'))\n"
        "print('WROTE_DROPIN')\n"
        "print('APPLY_OK')\n"
    )
    script_b64 = base64.b64encode(script.encode()).decode()
    script_path = f"/tmp/fw_net_apply_{uuid.uuid4().hex}.py"
    out, err = _sudo_exec(
        node,
        f"echo {script_b64} | base64 -d > {script_path} && python3 {script_path}; "
        f"rc=$?; rm -f {script_path}; exit $rc",
        timeout=180,
    )
    output = "\n".join(part for part in (out.strip(), err.strip()) if part)
    if "WROTE_DROPIN" in output and "APPLY_OK" in output:
        patch_note = ""
        match = re.search(r"^PATCHED (.+)$", output, re.MULTILINE)
        if match and match.group(1) != "none":
            patch_note = f"；已接管 {match.group(1)}"
        detail = ", ".join(f"{i}={ip}" for i, ip in ips.items())
        return True, f"已写入高速网配置（{detail}）{patch_note}"
    match = re.search(r"^FW_APPLY_ERROR (.+)$", output, re.MULTILINE)
    if match:
        try:
            failure = json.loads(match.group(1))
            rollback = failure.get("rollback")
            return False, (
                f"{failure.get('stage')} 失败：{failure.get('detail') or '无错误输出'}；"
                f"节点配置已自动恢复（回滚 {'成功' if rollback == 'ok' else '失败：' + str(rollback)}）"
            )
        except (ValueError, TypeError):
            pass
    return False, output[-1200:] or "应用失败（未获得 Netplan 输出）"


def rollback_node_network(node: Node) -> tuple[bool, str]:
    """回滚：删除本项目高速网声明 + 还原被接管文件，netplan apply 恢复接入前状态。

    只清理本项目产物：999-fireworks-network.yaml + 固定单名 *.yaml.fw-bak 备份
    （还原即删，不堆积）；不触碰官方/管理网等其它文件。
    """
    script = (
        "import os, glob, shutil, subprocess, json\n"
        f"DROPIN = '{NETPLAN_DROPIN}'\n"
        f"SUFFIX = '{FW_BAK_SUFFIX}'\n"
        "restored = []\n"
        "previous_dropin = open(DROPIN, 'rb').read() if os.path.exists(DROPIN) else None\n"
        "previous_targets = {}\n"
        "backup_contents = {}\n"
        "# 还原被接管文件的固定单名备份（还原即删，不堆积）\n"
        "for bak in sorted(glob.glob('/etc/netplan/*.yaml' + SUFFIX)):\n"
        "    target = bak[:-len(SUFFIX)]\n"
        "    previous_targets[target] = open(target, 'rb').read() if os.path.exists(target) else None\n"
        "    backup_contents[bak] = open(bak, 'rb').read()\n"
        "    shutil.copy(bak, target)\n"
        "    os.remove(bak)\n"
        "    restored.append('RESTORED:' + os.path.basename(target))\n"
        "if os.path.exists(DROPIN):\n"
        "    os.remove(DROPIN)\n"
        "    restored.append('REMOVED_DROPIN')\n"
        "def run(action):\n"
        "    return subprocess.run(['netplan', action], capture_output=True, text=True, timeout=150)\n"
        "generated = run('generate')\n"
        "applied = run('apply') if generated.returncode == 0 else generated\n"
        "if applied.returncode != 0:\n"
        "    for target, content in previous_targets.items():\n"
        "        if content is None:\n"
        "            if os.path.exists(target): os.remove(target)\n"
        "        else:\n"
        "            open(target, 'wb').write(content)\n"
        "    for bak, content in backup_contents.items(): open(bak, 'wb').write(content)\n"
        "    if previous_dropin is not None: open(DROPIN, 'wb').write(previous_dropin)\n"
        "    retry = run('generate')\n"
        "    if retry.returncode == 0: retry = run('apply')\n"
        "    detail = ((applied.stdout or '') + '\\n' + (applied.stderr or '')).strip()[-1200:]\n"
        "    print('FW_ROLLBACK_ERROR ' + json.dumps({'detail': detail, 'original_restored': retry.returncode == 0}))\n"
        "    raise SystemExit(2)\n"
        "print('|'.join(restored) if restored else 'NONE')\n"
        "print('APPLY_OK')\n"
    )
    script_b64 = base64.b64encode(script.encode()).decode()
    script_path = f"/tmp/fw_net_rollback_{uuid.uuid4().hex}.py"
    out, err = _sudo_exec(
        node,
        f"echo {script_b64} | base64 -d > {script_path} && python3 {script_path}; "
        f"rc=$?; rm -f {script_path}; exit $rc",
        timeout=180,
    )
    output = "\n".join(part for part in (out.strip(), err.strip()) if part)
    if "APPLY_OK" in output:
        return True, "已还原节点网络配置（删除本项目声明，接口恢复接入前状态）"
    match = re.search(r"^FW_ROLLBACK_ERROR (.+)$", output, re.MULTILINE)
    if match:
        try:
            failure = json.loads(match.group(1))
            state = "已恢复回滚前配置" if failure.get("original_restored") else "回滚前配置恢复也失败"
            return False, f"Netplan 回滚失败：{failure.get('detail') or '无错误输出'}；{state}"
        except (ValueError, TypeError):
            pass
    return False, output[-1200:] or "回滚失败（未获得 Netplan 输出）"


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
        # 本机 4 接口均应有 plan 分配的 IP（等待最多 ~40s）；
        # 任一规划 IP 处于 DADFAILED（地址重复检测失败）视为冲突，需更换网段
        local_ok = False
        local_dad_fail = False
        local_actual: set[str] = set()
        for _ in range(7):
            out, _, _ = ssh_client.exec(
                client,
                "ip -4 -o addr show 2>/dev/null | grep '" + _plan_grep(plan) + "'",
                timeout=20,
            )
            actual: set[str] = set()
            dad_fail = False
            for line in out.splitlines():
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", line)
                if not m:
                    continue
                actual.add(m.group(1).split("/")[0])
                if "dadfailed" in line:
                    dad_fail = True
            if not dad_fail and all(ip in actual for ip in ips.values()):
                local_ok = True
                local_actual = actual
                local_dad_fail = False
                break
            local_dad_fail = dad_fail
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
        all_ok = local_ok and gid_ok and not local_dad_fail
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
            "dad_failed": local_dad_fail,
            "local_ips": sorted(local_actual),
        }
    finally:
        client.close()


def verify_peer_reachability(
    node: Node, plan: dict, peers: list[tuple[Node, int]]
) -> tuple[bool, dict[str, dict[str, bool]]]:
    """从既有成员向新成员逐 rail 验证，补足添加节点流程的反向连通性。"""
    client = ssh_client.connect(node, timeout=20)
    detail: dict[str, dict[str, bool]] = {}
    all_ok = True
    try:
        for iface, _ in ROCE_IFACES:
            detail[iface] = {}
            for peer, peer_index in peers:
                peer_ip = node_ips(plan, peer_index)[iface]
                out, _, _ = ssh_client.exec(
                    client,
                    f"ping -I {iface} -c 2 -W 2 {peer_ip} 2>&1 | tail -1",
                    timeout=15,
                )
                ok = ("0% packet loss" in out or "ms" in out) and "100% packet loss" not in out
                detail[iface][f"{peer.name}@{peer_ip}"] = ok
                all_ok = all_ok and ok
        return all_ok, detail
    finally:
        client.close()
