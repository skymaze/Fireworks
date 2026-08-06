"""网络测试编排：在 from 节点起 server，在 to 节点跑 client，返回结果。

- ping     : from 节点 ping to 节点（仅 client）
- iperf3   : from 起 iperf3 -s -D，to 跑 iperf3 -c --json，最后清理 server
- ib_write_bw / ib_read_bw : from 起 perftest server，to 跑 client（server 单次自动退出）；
  自动走 RoCE 高速网 IP（node_roce_ip）与 HCA
"""

import asyncio

from ..models import Node
from . import agent_client

NETWORK_TEST_PORT = 5201


def _roce_info(node: Node) -> tuple[str | None, str | None]:
    """返回 (roce_ip, hca)。优先选带 RoCEv2 IP 与 GID 的 HCA。"""
    hw = node.hardware_info or {}
    for r in hw.get("roce") or []:
        if r.get("rocev2_ip") and r.get("gid_index") is not None:
            return r["rocev2_ip"], r.get("hca")
    for r in hw.get("roce") or []:
        if r.get("ipv4"):
            return r["ipv4"][0], r.get("hca")
    return None, None


async def run_network_test(
    from_node: Node,
    to_node: Node,
    tool: str,
    duration: int = 10,
    ib_device: str | None = None,
    roce_ip_override: str | None = None,
) -> dict:
    is_ib = tool in ("ib_write_bw", "ib_read_bw")
    roce_ip, auto_hca = _roce_info(from_node)
    if is_ib:
        if not ib_device:
            ib_device = auto_hca
        server_host = roce_ip_override or roce_ip or from_node.ip  # 走 RoCE 高速网（优先集群 plan IP）
    else:
        server_host = from_node.ip

    if tool == "ping":
        payload = {
            "role": "client",
            "tool": "ping",
            "server_host": to_node.ip,
            "count": min(duration, 20),
        }
        result = await agent_client.network_test(from_node, payload, duration=duration)
        return {"tool": "ping", "from": from_node.name, "to": to_node.name, **result}

    server_payload = {
        "role": "server",
        "tool": tool,
        "port": NETWORK_TEST_PORT,
        "ib_device": ib_device,
    }
    try:
        result = await agent_client.network_test(from_node, server_payload, duration=duration)
        if not result.get("started"):
            # agent 已返回但启动失败（如工具未安装/端口占用）：给出明确错误
            return {"tool": tool, "from": from_node.name, "to": to_node.name,
                    "error": f"启动 server 失败: {result.get('error') or '未知错误'}"}
    except Exception as e:  # noqa: BLE001
        return {"tool": tool, "from": from_node.name, "to": to_node.name, "error": f"启动 server 失败: {e}"}

    await asyncio.sleep(2)  # 等待 server 完成端口绑定

    try:
        client_payload = {
            "role": "client",
            "tool": tool,
            "server_host": server_host,
            "port": NETWORK_TEST_PORT,
            "duration": duration,
            "ib_device": ib_device,
        }
        result = await agent_client.network_test(to_node, client_payload, duration=duration)
        return {"tool": tool, "from": from_node.name, "to": to_node.name, **result}
    except Exception as e:  # noqa: BLE001
        return {"tool": tool, "from": from_node.name, "to": to_node.name,
                "error": f"测试执行失败: {e}"}
    finally:
        await agent_client.network_server_stop(from_node, {"tool": tool, "port": NETWORK_TEST_PORT})
