#!/usr/bin/env python3
"""DGX Spark Agent - 运行在受管节点宿主机上的轻量监控/管理服务。

功能:
  - 硬件信息采集 (GET /api/info)   : CPU/GPU/统一内存/磁盘/网卡/RoCE HCA + GID index
  - 实时指标采集 (GET /api/metrics): 温度/CPU/GPU/统一内存/磁盘/网络速率
  - 原始 nvidia-smi (GET /api/nvidia-smi)
  - 容器生命周期 (POST/GET /api/containers...): run/list/logs/pause/unpause/stop/start
  - Compose 编排 (POST /api/compose/up|down) : 与参考仓库的 docker compose 流程兼容
  - 网络测试 (POST /api/network/test)        : iperf3 / ib_write_bw / ib_read_bw / ping

依赖: fastapi uvicorn psutil
运行: uvicorn main:app --host 0.0.0.0 --port 9000
"""

import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

APP_VERSION = "0.3.1"


def resolve_workdir() -> Path:
    """工作目录：优先 FW_AGENT_WORKDIR，其次 /opt/fireworks-agent，最后用户目录/tmp。"""
    candidates = [
        os.environ.get("FW_AGENT_WORKDIR"),
        "/opt/fireworks-agent/work",
        str(Path.home() / ".fireworks-agent" / "work"),
        "/tmp/fireworks-agent/work",
    ]
    for c in candidates:
        if not c:
            continue
        try:
            p = Path(c)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            continue
    return Path("/tmp/fireworks-agent/work")


WORK_DIR = resolve_workdir()

app = FastAPI(title="DGX Spark Agent", version=APP_VERSION)
# CORS 收紧：Agent 只接受控制平面的服务端调用（无浏览器访问），不放行任何跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 鉴权：该节点的独立 token（由部署脚本写入 FW_AGENT_TOKEN）
#
# 所有端点（除 /api/health 探针）与 /ws/events 均要求携带合法 token：
# - Authorization: Bearer <token> 或 X-Agent-Token 或 ?token=；
# - 恒时比较防时序侧信道；
# - 未配置 FW_AGENT_TOKEN 时 fail closed：拒绝一切请求，
#   避免因忘记下发 token 导致 Agent 裸奔在网络上。
# ---------------------------------------------------------------------------

AGENT_TOKEN = os.environ.get("FW_AGENT_TOKEN", "").strip()

if not AGENT_TOKEN:
    print("[agent] 警告: 未设置 FW_AGENT_TOKEN，Agent 将拒绝所有请求（fail closed）", flush=True)


def _valid_token(candidate: str | None) -> bool:
    if not AGENT_TOKEN or not candidate:
        return False
    return hmac.compare_digest(str(candidate), AGENT_TOKEN)


def _token_from_headers(headers) -> str | None:
    """提取请求头中的 token：优先 Authorization: Bearer，其次 X-Agent-Token。"""
    auth = headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return (headers.get("X-Agent-Token") or "").strip() or None


@app.middleware("http")
async def auth_middleware(request, call_next):
    # liveness 探针放行（部署就绪检查/后端轮询用），不含敏感信息
    if request.url.path == "/api/health":
        return await call_next(request)
    # Agent 间归档流使用独立、短期且仅绑定单个 digest 的传输令牌；令牌由端点
    # 自己校验，不暴露节点长期管理 token，也不需要节点间 SSH 互信。
    if request.url.path.startswith(("/api/image/share/", "/api/model/share/")):
        return await call_next(request)
    candidate = _token_from_headers(request.headers) or request.query_params.get("token")
    if not _valid_token(candidate):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    return await call_next(request)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def run_cmd(cmd, timeout=30, cwd=None):
    """运行命令，返回 (stdout, returncode, stderr)。命令不存在返回 rc=127。"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        return r.stdout.strip(), r.returncode, r.stderr.strip()
    except FileNotFoundError:
        return "", 127, f"command not found: {cmd[0] if cmd else ''}"
    except subprocess.TimeoutExpired:
        return "", -1, f"timeout after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return "", -2, str(e)


def parse_csv(text):
    """nvidia-smi --format=csv,noheader,nounits 输出 -> list[dict]"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        rows.append(parts)
    return rows


def read_first(path):
    try:
        v = Path(path).read_text().strip()
        return v
    except OSError:
        return None


def _num(v, default=None):
    """把 nvidia-smi 的字符串数值转 float；处理 GB10 上的 N/A/空。"""
    s = str(v).strip()
    if s in ("", "N/A", "[N/A]", "nan", "NaN"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def ipv4_to_hex(ip):
    """192.168.1.10 -> c0a8010a（用于匹配 RoCEv2 GID 后缀）"""
    return "".join(f"{int(x):02x}" for x in ip.split("."))


# ---------------------------------------------------------------------------
# 硬件信息
# ---------------------------------------------------------------------------


def get_cpu_info():
    try:
        info = platform.processor() or platform.machine()
    except Exception:  # noqa: BLE001
        info = platform.machine()
    freq = None
    try:
        f = psutil.cpu_freq()
        if f and f.current:
            freq = round(f.current, 1)
    except Exception:  # noqa: BLE001 部分平台/版本无频率数据
        freq = None
    return {
        "model": info,
        "physical_cores": psutil.cpu_count(logical=False) or os.cpu_count(),
        "logical_cores": os.cpu_count(),
        "freq_mhz": freq,
    }


def get_gpus():
    """nvidia-smi 查询 GPU 基本信息（含时钟节流原因，驱动不支持时自动回退基础列）。"""
    global _throttle_supported
    base = (
        "index,name,uuid,memory.total,memory.free,temperature.gpu,"
        "utilization.gpu,power.draw,power.limit,compute_cap,driver_version"
    )
    fields = (base + ",clocks_throttle_reasons.active"
              if _throttle_supported is not False else base)
    out, rc, _err = run_cmd(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
        timeout=15,
    )
    if rc != 0 and _throttle_supported is None:
        # 驱动不支持节流列 -> 回退基础列（仅首次探测，避免每轮双查询）
        _throttle_supported = False
        out, rc2, _ = run_cmd(
            ["nvidia-smi", f"--query-gpu={base}", "--format=csv,noheader,nounits"],
            timeout=15,
        )
        if rc2 == 0:
            rc = 0
    elif rc == 0:
        _throttle_supported = True
    if rc != 0:
        return []
    has_throttle = _throttle_supported is not False
    gpus = []
    for p in parse_csv(out):
        try:
            gpus.append(
                {
                    "index": int(p[0]),
                    "name": p[1].replace("NVIDIA ", ""),
                    "uuid": p[2],
                    "memory_total": _num(p[3]),            # MiB；GB10 上可能为 N/A
                    "memory_free": _num(p[4]),
                    "temperature": _num(p[5]),
                    "utilization": _num(p[6]),
                    "power_draw": _num(p[7]),
                    "power_limit": _num(p[8]),
                    "compute_cap": p[9],
                    "driver_version": p[10],
                }
            )
            if has_throttle and len(p) > 11:
                gpus[-1]["throttle_reasons"] = p[11].strip()
        except (ValueError, IndexError):
            continue
    return gpus


def get_disks():
    disks = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("", "squashfs", "overlay"):
            continue
        if part.mountpoint.startswith(("/sys", "/proc", "/dev", "/var/lib/docker")):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append(
                {
                    "mount": part.mountpoint,
                    "device": part.device,
                    "fstype": part.fstype,
                    "total": u.total,
                    "used": u.used,
                    "free": u.free,
                    "percent": u.percent,
                }
            )
        except OSError:
            continue
    return disks


def get_interfaces():
    """网卡列表（仅物理 + 有地址的），含 IPv4/MAC/MTU/up。"""
    ifs = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        if name in ("lo", "docker0", "br-", "veth"):
            if not name.startswith(("en", "eth", "ib", "wlan", "bond", "team")):
                continue
        ipv4 = [a.address for a in addrs if a.family == socket.AF_INET]
        mac = next((a.address for a in addrs if a.family == psutil.AF_LINK), None)
        st = stats.get(name)
        ifs.append(
            {
                "name": name,
                "ipv4": ipv4,
                "mac": mac,
                "mtu": st.mtu if st else None,
                "up": bool(st.isup) if st else False,
                "speed_mbps": st.speed if st else None,
                # PCIe 地址（dcard 总线标识，如 0000:01:00.0）；非 PCI 网卡为 None
                "pci": _pci_addr(name),
            }
        )
    return ifs


def _pci_addr(ifname: str) -> str | None:
    """读取接口的 PCIe 总线地址（/sys/class/net/<if>/device）。"""
    try:
        return Path(f"/sys/class/net/{ifname}/device").resolve().name
    except (OSError, RuntimeError):
        return None


def get_roce():
    """探测 RoCE/IB HCA、关联网卡、RoCEv2 GID index。

    GID index 每节点/HCA 不同且重启后漂移（参考仓库 docs/SETUP.md 的经验），
    这里按 /sys/class/infiniband/<hca>/ports/*/gids/* 与接口 IPv4 匹配解析。
    """
    ib_dir = Path("/sys/class/infiniband")
    if not ib_dir.exists():
        return []
    ifaces = get_interfaces()
    ipv4_by_if = {i["name"]: i["ipv4"] for i in ifaces}
    roce = []
    for hca in sorted(ib_dir.iterdir()):
        device_net = hca / "device" / "net"
        netdevs = [d.name for d in device_net.iterdir()] if device_net.exists() else []
        ports_dir = hca / "ports"
        if not ports_dir.exists():
            continue
        for port in sorted(ports_dir.iterdir(), key=lambda p: p.name):
            if not port.name.isdigit():
                continue
            link_layer = read_first(port / "link_layer") or "Unknown"
            rate = read_first(port / "rate") or "Unknown"
            state = read_first(port / "state") or ""
            # 匹配 RoCEv2 GID（格式 0000:0000:0000:0000:0000:ffff:<ipv4 hex>）
            gid_index = None
            matched_ip = None
            gids_dir = port / "gids"
            if gids_dir.exists():
                for gid_file in sorted(gids_dir.iterdir(), key=lambda p: p.name):
                    if not gid_file.name.isdigit():
                        continue
                    gid = read_first(gid_file) or ""
                    hex_suffix = gid.replace(":", "")[-8:]
                    if len(hex_suffix) != 8 or not re.fullmatch(r"[0-9a-fA-F]{8}", hex_suffix):
                        continue
                    for iface, ips in ipv4_by_if.items():
                        if ips and ipv4_to_hex(ips[0]).lower() == hex_suffix.lower():
                            gid_index = int(gid_file.name)
                            matched_ip = ips[0]
                            break
                    if gid_index is not None:
                        break
            netdev = netdevs[0] if netdevs else None
            roce.append(
                {
                    "hca": hca.name,
                    "port": int(port.name),
                    "netdev": netdev,
                    "link_layer": link_layer,
                    "rate": rate,
                    "state": state,
                    "gid_index": gid_index,
                    "ipv4": ipv4_by_if.get(netdev, []) if netdev else [],
                    "rocev2_ip": matched_ip,
                }
            )
    return roce


def get_docker_info():
    out, rc, _ = run_cmd(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10)
    return {"available": rc == 0, "version": out or None}


def get_tools():
    return {
        "iperf3": shutil.which("iperf3") is not None,
        "ib_write_bw": shutil.which("ib_write_bw") is not None,
        "ib_read_bw": shutil.which("ib_read_bw") is not None,
        "ibv_devinfo": shutil.which("ibv_devinfo") is not None,
        "ping": shutil.which("ping") is not None,
        "nccl_tests": shutil.which("all_reduce_perf") is not None,
    }


@app.get("/api/info")
def api_info():
    gpus = get_gpus()
    vm = psutil.virtual_memory()
    # GB10 上 nvidia-smi 的 FB Memory 通常为 N/A，统一内存以系统内存为准
    gpu_mem_total = sum(g["memory_total"] for g in gpus if g.get("memory_total"))
    unified_total = gpu_mem_total if gpu_mem_total else vm.total
    try:
        if hasattr(platform, "freedesktop_os_release"):
            distro = platform.freedesktop_os_release().get("PRETTY_NAME")
        else:
            distro = None
    except OSError:  # 非 Linux（如 macOS）无 /etc/os-release
        distro = None
    return {
        "agent_version": APP_VERSION,
        "capabilities": ["image_peer_transfer_v1", "model_peer_transfer_v1"],
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "distro": distro,
        "arch": platform.machine(),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "cpu": get_cpu_info(),
        "memory": {"total": vm.total, "available": vm.available},
        "unified_memory": {"total": unified_total},
        "gpus": gpus,
        "disks": get_disks(),
        "interfaces": get_interfaces(),
        "roce": get_roce(),
        "docker": get_docker_info(),
        "tools": get_tools(),
    }


# ---------------------------------------------------------------------------
# 指标采集
# ---------------------------------------------------------------------------

# 网络速率与 CPU 百分比需要跨调用差分
_state = {"net_prev": psutil.net_io_counters(pernic=True), "net_ts": time.time()}
psutil.cpu_percent(interval=None)  # 预热，避免首次返回 0

# nvidia-smi 是否支持 clocks_throttle_reasons 列（None=未探测；不支持时避免每轮双查询）
_throttle_supported: bool | None = None


def get_temperatures(gpus):
    """CPU/系统温度来自 thermal zone，GPU 温度来自 gpus。"""
    zones = []
    zone_dir = Path("/sys/class/thermal")
    if zone_dir.exists():
        for z in sorted(zone_dir.glob("thermal_zone*"), key=lambda p: p.name):
            t = read_first(z / "temp")
            typ = read_first(z / "type") or "unknown"
            try:
                zones.append({"type": typ, "celsius": round(int(t) / 1000, 1)})
            except (ValueError, TypeError):
                continue
    # 取非 GPU zone 的最高值作为 CPU/SoC 温度（GB10 上通常是 SoC 封装）
    cpu_temp = None
    for z in zones:
        if "gpu" not in z["type"].lower() and "pch" not in z["type"].lower():
            cpu_temp = max(cpu_temp or 0, z["celsius"])
    return {
        "cpu": cpu_temp,
        "gpus": [g["temperature"] for g in gpus],
        "zones": zones,
    }


def collect_metrics() -> dict:
    """采集一次完整指标（HTTP /api/metrics 与 WS 推送共用）。"""
    gpus = get_gpus()
    vm = psutil.virtual_memory()
    now = time.time()

    net = psutil.net_io_counters(pernic=True)
    dt = max(now - _state["net_ts"], 1e-6)
    prev = _state["net_prev"]
    rx_rate = tx_rate = 0.0
    per_if = {}
    for name, cur in net.items():
        p = prev.get(name)
        if p and name not in ("lo",):
            r = max((cur.bytes_recv - p.bytes_recv) / dt, 0)
            t = max((cur.bytes_sent - p.bytes_sent) / dt, 0)
            per_if[name] = {"rx_bps": round(r), "tx_bps": round(t)}
            rx_rate += r
            tx_rate += t
    _state["net_prev"] = net
    _state["net_ts"] = now

    gpu_agg = {"count": len(gpus), "utilization": None, "mem_used": 0, "mem_total": 0}
    if gpus:
        utils = [g["utilization"] for g in gpus if g.get("utilization") is not None]
        if utils:
            gpu_agg["utilization"] = round(sum(utils) / len(utils), 1)
        mems = [(g["memory_total"], g["memory_free"]) for g in gpus
                if g.get("memory_total") is not None and g.get("memory_free") is not None]
        if mems:
            # nvidia-smi memory 单位为 MiB，统一换算为字节
            gpu_agg["mem_used"] = sum((mt - mf) for mt, mf in mems) * 1024 * 1024
            gpu_agg["mem_total"] = sum(mt for mt, _ in mems) * 1024 * 1024
        else:
            # GB10 无 FB Memory 数据（统一内存架构，无独立显存）：
            # 以系统内存作为统一内存占用（DGX Spark = 128GB 统一内存，CPU/GPU 共享）
            gpu_agg["mem_used"] = vm.used
            gpu_agg["mem_total"] = vm.total

    return {
        "ts": now,
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory": {
            "total": vm.total,
            "used": vm.used,
            "percent": vm.percent,
        },
        "gpu": gpu_agg,
        "gpus": gpus,
        "temperatures": get_temperatures(gpus),
        "disks": get_disks(),
        "network": {"rx_bps": round(rx_rate), "tx_bps": round(tx_rate), "per_if": per_if},
        "load": psutil.getloadavg(),
    }


@app.get("/api/metrics")
def api_metrics():
    return collect_metrics()


@app.get("/api/nvidia-smi")
def api_nvidia_smi():
    out, rc, err = run_cmd(["nvidia-smi"], timeout=20)
    if rc != 0:
        raise HTTPException(502, f"nvidia-smi 不可用: {err or out}")
    return {"output": out}


@app.get("/api/health")
def api_health():
    return {"status": "ok", "version": APP_VERSION, "time": time.time()}


@app.get("/api/http-get")
def api_http_get(url: str, timeout: int = 10):
    """控制平面经由 Agent 发起的 HTTP 探活（如 vLLM /v1/models）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(2048).decode("utf-8", "replace")
            return {"status": r.status, "body": body}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# 推理服务统计（LLM inference stats）：被动读取真实推理流量（vLLM /metrics）
#
# 不向推理服务发送合成请求（占性能并扰动被观测指标）。周期读取 vLLM /metrics，
# 返回**原始累计快照**（计数器 / KV gauge / 完整直方图 sum+count+buckets），
# **不做差分与聚合**——agent 保持无状态，差分与统计由控制平面对相邻样本完成：
#   - tokens_per_sec / 生成/提示/请求增量 = 相邻样本计数器差分 ÷ Δt
#   - TTFT/E2E        = 直方图 _sum/_count/_bucket 差分后取分位
#   - kv_cache_percent = 实时 gauge（0..1 -> %）
# 非 vLLM 后端或 /metrics 不可用时快照字段为 None；"无流量不落点"由写侧决定。
# ---------------------------------------------------------------------------


def _http_get_short(url: str, timeout: int = 3, limit: int = 4096) -> tuple[int, str]:
    """轻量 GET（探后端用）；limit 限制读取字节（/metrics 较大，探测时放大）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(limit).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return 0, ""


def _detect_backend(url_base: str) -> str:
    """探测推理后端类型：/metrics 含 vllm -> vllm；否则 /v1/models 200 -> openai；未知 unknown。"""
    status, body = _http_get_short(f"{url_base}/metrics", limit=512 * 1024)
    if status == 200 and "vllm" in body:
        return "vllm"
    status, _ = _http_get_short(f"{url_base}/v1/models")
    return "openai" if status == 200 else "unknown"


# vLLM /metrics 指标名（已对照 vLLM 官方源码确认，前缀均为 "vllm:"；0.11 前后部分
# 改名，故按"指标名后缀"匹配以兼容新旧版本）：
#   计数器（累计 total，带 label 的多系列求和）：
#     generation/prompt_tokens_total、request_success_total
#   KV 用量 gauge：kv_cache_usage_perc（v0.11+）∪ gpu_cache_usage_perc（≤v0.10.2）
#   直方图三件套 _bucket/_sum/_count：time_to_first_token_seconds、
#     e2e_request_latency_seconds、time_per_output_token_seconds
#     （新版 alias：request_time_per_output_token_seconds）
_COUNTER_SUFFIXES = (
    "generation_tokens_total",
    "prompt_tokens_total",
    "request_success_total",
)
_KV_SUFFIXES = ("kv_cache_usage_perc", "gpu_cache_usage_perc")
_METRICS_READ_LIMIT = 8 * 1024 * 1024
# 直方图族：指标名后缀 -> 归属字段（新旧别名并到同一字段；长名在前避免短名抢先）
_HIST_SUFFIXES = {
    "request_time_per_output_token_seconds": "tpot",
    "time_per_output_token_seconds": "tpot",
    "time_to_first_token_seconds": "ttft",
    "e2e_request_latency_seconds": "e2e",
}


def _metric_name_and_le(line: str) -> tuple[str, str | None]:
    """metrics 一行 -> (去掉标签的指标名, le 标签值/None)。

    无标签行（如 _sum/_count）首个空白 token 即指标名，不能整行走 partition("{"）。
    """
    head = line.split()[0]
    name, _, labels = head.partition("{")
    le = None
    if labels:
        m = re.search(r'le="([^"]*)"', labels)
        if m:
            le = m.group(1)
    return name.strip(), le


def _bound_seconds(bound: str) -> float | None:
    """直方图桶上界 le 字符串 -> 秒数值；+Inf -> inf；解析失败返回 None。"""
    b = bound.strip()
    if b in ("+Inf", "+inf", "inf"):
        return float("inf")
    try:
        return float(b)
    except ValueError:
        return None


def _collect_metrics_snapshot(body: str) -> dict | None:
    """解析 vLLM /metrics 文本为累计快照（供区间差分）。

    返回 {counters: {后缀: 累计和}, kv_percent: float|None,
         hist: {字段: {sum, count, buckets: [(上界秒, 累计数), ...]}}}。
    一个 vllm 指标都解析不到（非 vLLM / 未开 metrics）返回 None。
    """
    out: dict = {
        "counters": {},
        "kv_percent": None,
        "hist": {},
    }
    hit = False
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, le = _metric_name_and_le(line)
        if not name.startswith(("vllm:", "vllm_")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            value = float(parts[-1])
        except ValueError:
            continue
        # 计数器：按后缀匹配，多标签系列求和（counter cumulative）
        for suffix in _COUNTER_SUFFIXES:
            if name.endswith(suffix):
                out["counters"][suffix] = out["counters"].get(suffix, 0.0) + value
                hit = True
                break
        # KV 用量 gauge：0..1 -> %，取多系列最大值
        for suffix in _KV_SUFFIXES:
            if name.endswith(suffix):
                out["kv_percent"] = max(out["kv_percent"] or 0.0, value * 100.0)
                hit = True
                break
        # 直方图三件套：归组到字段（长后缀优先，命中即断）
        for suffix, field in _HIST_SUFFIXES.items():
            if name.endswith(suffix + "_bucket"):
                secs = _bound_seconds(le or "+Inf")
                if secs is None:
                    break
                out["hist"].setdefault(field, {"sum": 0.0, "count": 0.0, "buckets": []})
                out["hist"][field]["buckets"].append((secs, value))
                hit = True
                break
            if name.endswith(suffix + "_sum"):
                out["hist"].setdefault(field, {"sum": 0.0, "count": 0.0, "buckets": []})
                out["hist"][field]["sum"] += value
                hit = True
                break
            if name.endswith(suffix + "_count"):
                out["hist"].setdefault(field, {"sum": 0.0, "count": 0.0, "buckets": []})
                out["hist"][field]["count"] += value
                hit = True
                break
    if not hit:
        return None
    for field, h in out["hist"].items():
        if h["buckets"]:
            h["buckets"].sort(key=lambda b: b[0])  # 按上界升序，保证与同源快照对齐
    return out


def _collect_live_stats(url_base: str, timeout: float) -> tuple[str, dict | None]:
    """读取 vLLM /metrics -> (backend, 累计快照/None)。

    /metrics 含 vllm 且能解析出指标 -> (vllm, snap)；取不到/无 vllm 指标时回退只做
    /v1/models 存活检查，backend 为 openai/unknown，快照为 None（无统计产出）。
    """
    # vLLM 的直方图和多 label 系列可能让 /metrics 超过 512 KiB；截断会使排在
    # 后部的 KV gauge 或直方图静默缺失。使用有界 8 MiB 读取覆盖实际指标页，
    # 同时避免异常服务返回无界响应。
    status, body = _http_get_short(
        f"{url_base}/metrics", timeout=timeout, limit=_METRICS_READ_LIMIT
    )
    if status == 200 and "vllm" in body:
        return "vllm", _collect_metrics_snapshot(body)
    status, _ = _http_get_short(f"{url_base}/v1/models", timeout=timeout)
    return ("openai" if status == 200 else "unknown"), None


class LlmStatsRequest(BaseModel):
    """推理服务统计入参：被动读取 vLLM /metrics，不发送合成推理请求。"""

    url_base: str
    timeout: float = 8


@app.post("/api/inference/stats")
def api_inference_stats(req: LlmStatsRequest) -> dict:
    """控制平面经 Agent 读取推理服务 /metrics 的**原始累计快照**（无状态）。

    不做任何差分/聚合：返回累计计数器、KV gauge 与完整直方图（sum/count/buckets，
    均为单调累计），差分与统计由控制平面对相邻样本完成。非 vLLM 后端或
    /metrics 不可用时仅给出 backend 存活判定，快照字段为 None。

    直方图 +Inf 桶上界归一化为 ``null``（float('inf') 会序列化成非法的 ``Infinity``，
    浏览器 JSON.parse 失败）；前端把 null 视为无穷（末桶）。
    """
    url_base = req.url_base.rstrip("/")
    if not url_base:
        return {"ok": False, "error": "url_base 必填"}
    backend, snap = _collect_live_stats(url_base, req.timeout)
    if snap is None:
        return {
            "ok": True,
            "backend": backend,
            "generation_tokens_total": None,
            "prompt_tokens_total": None,
            "request_success_total": None,
            "kv_cache_percent": None,
            "ttft": None,
            "e2e": None,
        }

    def json_hist(h):
        if not h:
            return None
        return {
            "sum": h["sum"],
            "count": h["count"],
            "buckets": [[None if b == float("inf") else b, c] for b, c in h["buckets"]],
        }

    counters = snap["counters"]
    hist = snap["hist"]
    return {
        "ok": True,
        "backend": backend,
        "generation_tokens_total": counters.get("generation_tokens_total"),
        "prompt_tokens_total": counters.get("prompt_tokens_total"),
        "request_success_total": counters.get("request_success_total"),
        "kv_cache_percent": snap["kv_percent"],
        "ttft": json_hist(hist.get("ttft")),
        "e2e": json_hist(hist.get("e2e")),
    }


# ---------------------------------------------------------------------------
# 推理服务正基准测试（decode benchmark）：并发 streaming decode tok/s 压测
# ---------------------------------------------------------------------------


class LlmBenchmarkRequest(BaseModel):
    """推理服务并发 decode 压测入参。"""

    url_base: str
    model: str = "default"
    concurrency: int = 8
    num_requests: int = 32
    max_tokens: int = 64
    timeout: float = 120


def _bench_one(url_base: str, model: str, max_tokens: int, timeout: float,
               prompt: str) -> dict:
    """单条并发请求的原始测量（流式）：TTFT/E2E/输出 token 数/ITL 分位。"""
    import json as _json
    import urllib.request

    req = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.monotonic()
    token_times: list[float] = []
    output_tokens = prompt_tokens = 0
    try:
        request = urllib.request.Request(
            f"{url_base}/v1/chat/completions", data=_json.dumps(req).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = _json.loads(payload)
                except ValueError:
                    continue
                usage = chunk.get("usage")
                if usage:
                    output_tokens = usage.get("completion_tokens") or output_tokens
                    prompt_tokens = usage.get("prompt_tokens") or prompt_tokens
                choices = chunk.get("choices") or []
                if choices:
                    content = ((choices[0].get("delta") or {})).get("content")
                    if content:
                        token_times.append(time.monotonic())
        t_end = time.monotonic()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if not token_times:
        return {"ok": False, "error": "无输出 token"}
    tokens = output_tokens or len(token_times)
    ttft = token_times[0] - t0
    e2e = t_end - t0
    if len(token_times) >= 2:
        itls = sorted(b - a for a, b in zip(token_times, token_times[1:]))
        itl_p50 = itls[len(itls) // 2]
        itl_p95 = itls[min(len(itls) - 1, int(len(itls) * 0.95))]
    else:
        itl_p50 = itl_p95 = None
    return {"ok": True, "ttft": ttft, "e2e": e2e, "tokens": tokens,
            "itl_p50": itl_p50, "itl_p95": itl_p95,
            "t_start": t0, "t_end": t_end}


def _pct(sorted_list: list[float], p: float) -> float | None:
    if not sorted_list:
        return None
    i = min(len(sorted_list) - 1, int(len(sorted_list) * p))
    return sorted_list[i]


def _aggregate_benchmark(url_base: str, results: list[dict],
                         concurrency: int, num: int) -> dict:
    """把单条请求原始测量聚合为压测汇总（独立函数便于单测）。"""
    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    if not ok:
        return {"ok": False, "concurrency": concurrency, "num_requests": num,
                "succeeded": 0, "failed": len(failed),
                "error": (failed[0].get("error") if failed else "全部请求失败")}
    total_tokens = sum(r["tokens"] for r in ok)
    start = min(r["t_start"] for r in ok)
    end = max(r["t_end"] for r in ok)
    span = max(end - start, 1e-9)
    ttfts = sorted(r["ttft"] for r in ok)
    e2es = sorted(r["e2e"] for r in ok)
    itls = sorted(r["itl_p50"] for r in ok if r["itl_p50"] is not None)
    e2e_p50 = _pct(e2es, 0.5)
    per_request = [{
        "ttft_ms": round(r["ttft"] * 1000, 1),
        "e2e_ms": round(r["e2e"] * 1000, 1),
        "tokens": r["tokens"],
        "itl_p50_ms": round(r["itl_p50"] * 1000, 1) if r["itl_p50"] is not None else None,
    } for r in ok]
    return {
        "ok": True,
        "backend": _detect_backend(url_base),
        "concurrency": concurrency,
        "num_requests": num,
        "succeeded": len(ok),
        "failed": len(failed),
        "total_tokens": total_tokens,
        "tokens_per_sec": round(total_tokens / span, 1),
        "ttft_p50_ms": round(_pct(ttfts, 0.5) * 1000, 1),
        "ttft_p95_ms": round(_pct(ttfts, 0.95) * 1000, 1),
        "e2e_p50_ms": round(e2e_p50 * 1000, 1),
        "e2e_p95_ms": round(_pct(e2es, 0.95) * 1000, 1),
        "latency_p50_ms": round(e2e_p50 * 1000, 1),
        "latency_p95_ms": round(_pct(e2es, 0.95) * 1000, 1),
        "itl_p50_ms": round(_pct(itls, 0.5) * 1000, 1) if itls else None,
        "itl_p95_ms": round(_pct(itls, 0.95) * 1000, 1) if itls else None,
        "per_request": per_request,
    }


@app.post("/api/probe/benchmark")
def api_probe_benchmark(req: LlmBenchmarkRequest) -> dict:
    """推理服务并发 streaming decode 吞吐压测（sparkDash decode benchmark 同源思路）。"""
    import concurrent.futures

    url_base = req.url_base.rstrip("/")
    if not url_base:
        return {"ok": False, "error": "url_base 必填"}
    concurrency = max(1, min(req.concurrency, 64))
    num = max(1, req.num_requests)
    prompt = "基准测试：请用中文生成一段通顺的短文。"
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(_bench_one, url_base, req.model, req.max_tokens,
                          req.timeout, prompt)
                for _ in range(num)]
        for f in concurrent.futures.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:  # noqa: BLE001
                results.append({"ok": False, "error": str(e)})
    return _aggregate_benchmark(url_base, results, concurrency, num)


# ---------------------------------------------------------------------------
# 容器生命周期（docker CLI）
# ---------------------------------------------------------------------------


def docker_ps_all():
    out, rc, err = run_cmd(
        ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], timeout=15
    )
    if rc != 0:
        raise HTTPException(502, f"docker ps 失败: {err}")
    containers = []
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(
            {
                "id": c.get("ID"),
                "name": c.get("Names"),
                "image": c.get("Image"),
                "state": c.get("State"),
                "status": c.get("Status"),
                "created": c.get("CreatedAt"),
            }
        )
    return containers


@app.get("/api/containers")
def list_containers():
    return {"containers": docker_ps_all()}


@app.get("/api/containers/{name}/logs")
def container_logs(name: str, tail: int = 200):
    out, rc, err = run_cmd(
        ["docker", "logs", "--tail", str(tail), name], timeout=20
    )
    if rc != 0:
        raise HTTPException(404, f"容器 {name} 日志获取失败: {err}")
    return {"name": name, "logs": out}


def _log_segments(data: bytes, buf: bytes, prev_update: bool):
    """把日志字节流按 \n / \r 切分为日志段。

    - \n 结尾   -> 完整行（通常 update=False；若前一段是原地刷新链，则该段视为
                  刷新行最终态，update=True，但换行后必须结束刷新链）
    - \r 结尾   -> 原地刷新行（进度类本行覆盖，update=True）
    - \r\n      -> 完整行（通常 update=False；同样可结束原地刷新链）

    返回 (段列表[(bytes, update)], 剩余未完成缓冲, 最后一段的 update 标志)。
    空段（连续分隔符）跳过且不改变 update 标志。
    """
    buf += data
    out: list[tuple[bytes, bool]] = []
    while True:
        idx_n = buf.find(b"\n")
        idx_r = buf.find(b"\r")
        if idx_n == -1 and idx_r == -1:
            break
        if idx_n != -1 and (idx_r == -1 or idx_n < idx_r):
            seg, buf = buf[:idx_n], buf[idx_n + 1:]
            if seg:
                out.append((seg, prev_update))
            # 无论本段是否为空，换行都结束原地刷新链；否则一次进度输出会让
            # 后续所有普通日志永久携带 update=true，前端只会反复替换末行。
            prev_update = False
        elif idx_n == idx_r + 1:  # \r\n
            seg, buf = buf[:idx_r], buf[idx_n + 1:]
            if seg:
                out.append((seg, prev_update))
            prev_update = False
        else:  # 仅 \r：进度类原地刷新
            seg, buf = buf[:idx_r], buf[idx_r + 1:]
            if seg:
                out.append((seg, True))
                prev_update = True
    return out, buf, prev_update


class ContainerActionRequest(BaseModel):
    action: str  # pause | unpause | stop | start | restart


@app.post("/api/containers/{name}/action")
def container_action(name: str, req: ContainerActionRequest):
    if req.action not in ("pause", "unpause", "stop", "start", "restart"):
        raise HTTPException(400, f"不支持的动作: {req.action}")
    out, rc, err = run_cmd(["docker", req.action, name], timeout=60)
    if rc != 0:
        raise HTTPException(502, f"docker {req.action} 失败: {err or out}")
    return {"name": name, "action": req.action, "ok": True}


# ---------------------------------------------------------------------------
# Compose 编排（任务发布）
# ---------------------------------------------------------------------------


class ComposeUpRequest(BaseModel):
    project: str
    compose_yaml: str
    env: dict[str, str] = Field(default_factory=dict)


def _compose_dir(project: str) -> Path:
    d = WORK_DIR / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate_project(project) -> str:
    """project 名校验：仅允许安全字符，且不允许 "." / ".." 等路径段（防越出工作目录）。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", project or ""):
        raise HTTPException(400, f"非法 project 名: {project!r}")
    if project in (".", "..") or project.startswith(".."):
        raise HTTPException(400, f"非法 project 名: {project!r}")
    return project


@app.post("/api/compose/up")
def compose_up(req: ComposeUpRequest):
    project = _validate_project(req.project)
    # env key 白名单 + 剥离 CR/LF：防换行注入 compose 插值产生额外环境变量
    env_lines = []
    for k, v in req.env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise HTTPException(400, f"非法环境变量名: {k!r}")
        env_lines.append(f"{k}={str(v).replace(chr(10), '').replace(chr(13), '')}")
    # 所有输入校验通过后才落盘，避免 400 请求覆盖已有任务配置。
    d = _compose_dir(project)
    (d / "compose.yml").write_text(req.compose_yaml, encoding="utf-8")
    (d / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    out, rc, err = run_cmd(
        ["docker", "compose", "-p", project, "-f", "compose.yml",
         "up", "-d", "--remove-orphans"],
        timeout=600,
        cwd=d,
    )
    if rc != 0:
        raise HTTPException(502, f"docker compose up 失败: {err or out}")
    return {"project": project, "ok": True, "output": out}


@app.post("/api/compose/down")
def compose_down(req: dict):
    project = _validate_project(req.get("project", ""))
    out, rc, err = run_cmd(
        ["docker", "compose", "-p", project, "-f", "compose.yml", "down"],
        timeout=120,
        cwd=_compose_dir(project),
    )
    if rc != 0:
        raise HTTPException(502, f"docker compose down 失败: {err or out}")
    return {"project": project, "ok": True}


@app.post("/api/compose/ps")
def compose_ps(req: dict):
    project = _validate_project(req.get("project", ""))
    out, rc, err = run_cmd(
        ["docker", "compose", "-p", project, "-f", "compose.yml", "ps", "-a", "--format", "json"],
        timeout=30,
        cwd=_compose_dir(project),
    )
    if rc != 0:
        # 失败不再静默返回空列表（会掩盖容器状态异常，task_monitor 永不更新）
        raise HTTPException(502, f"docker compose ps 失败: {err or out[:200]}")
    containers = []
    for line in out.splitlines():
        try:
            c = json.loads(line)
            containers.append(
                {"name": c.get("Name"), "state": c.get("State"),
                 "status": c.get("Status"), "service": c.get("Service")}
            )
        except json.JSONDecodeError:
            continue
    return {"project": project, "containers": containers}


# ---------------------------------------------------------------------------
# 网络测试（控制平面编排 server/client 两端）
# ---------------------------------------------------------------------------


class NetworkTestRequest(BaseModel):
    role: str  # server | client
    tool: str  # iperf3 | ib_write_bw | ib_read_bw | ping
    server_host: str | None = None
    port: int = 5201
    duration: int = 10
    ib_device: str | None = None
    count: int = 4  # ping 次数


@app.post("/api/network/server-stop")
def network_server_stop(req: dict):
    """停止本节点上的网络测试 server 进程（iperf3 -D / perftest）。"""
    port = req.get("port", 5201)
    tool = req.get("tool", "iperf3")
    killed = []
    # 回收登记在册的 server 进程（含 wait，避免僵尸）
    for key in list(_test_servers):
        ptool, pport = key
        if ptool == tool or pport == port:
            p = _test_servers.pop(key, None)
            if p:
                _terminate_proc(p)
                killed.append({"tracked": f"{ptool}:{pport}"})
    patterns = {
        "iperf3": f"iperf3 -s -p {port}",
        "ib_write_bw": "ib_write_bw",
        "ib_read_bw": "ib_read_bw",
    }
    pat = patterns.get(tool)
    if pat:
        out, rc, _ = run_cmd(["pkill", "-f", pat])
        killed.append({"pattern": pat, "rc": rc, "output": out})
    return {"ok": True, "killed": killed}


@app.post("/api/network/test")
def network_test(req: NetworkTestRequest):
    # ib 工具未指定 HCA 时自动探测第一个 InfiniBand 设备
    ib_device = req.ib_device
    if not ib_device and req.tool in ("ib_write_bw", "ib_read_bw"):
        out, _, _ = run_cmd(["ls", "/sys/class/infiniband"])
        ib_device = out.splitlines()[0] if out else None
    if req.role == "server":
        if req.tool == "iperf3":
            out, rc, err = run_cmd(
                ["iperf3", "-s", "-p", str(req.port), "-D", "--logfile",
                 f"/tmp/fireworks-iperf3-{req.port}.log"],
                timeout=15,
            )
            return {"role": "server", "tool": req.tool, "started": rc == 0,
                    "output": out, "error": err if rc != 0 else None}
        if req.tool in ("ib_write_bw", "ib_read_bw"):
            # 清理残留的 perftest 进程（避免端口占用），再启动 server
            _reap_test_server(req.tool, req.port)
            run_cmd(["pkill", "-f", f"{req.tool}.*-p {req.port}"])
            time.sleep(0.5)
            cmd = [req.tool, "-d", ib_device, "-p", str(req.port)] if ib_device else [req.tool, "-p", str(req.port)]
            # 输出重定向到文件：PIPE 且无读取者时，输出超 64KB 管道缓冲会阻塞 server
            logf = open(f"/tmp/fireworks-{req.tool}-{req.port}.log", "w")
            p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                 text=True, start_new_session=True)
            _test_servers[(req.tool, req.port)] = p
            return {"role": "server", "tool": req.tool, "pid": p.pid, "started": True}
        raise HTTPException(400, f"tool {req.tool} 不支持 server 角色")

    # client
    if not req.server_host:
        raise HTTPException(400, "缺少 server_host")
    if req.tool == "iperf3":
        out, rc, err = run_cmd(
            ["iperf3", "-c", req.server_host, "-p", str(req.port),
             "-t", str(req.duration), "--json"],
            timeout=req.duration + 30,
        )
        if rc != 0:
            raise HTTPException(502, f"iperf3 失败: {err or out}")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = None
        return {"role": "client", "tool": req.tool, "data": data, "raw": out}
    if req.tool in ("ib_write_bw", "ib_read_bw"):
        cmd = [req.tool, "-d", ib_device, "-p", str(req.port), req.server_host] if ib_device else [req.tool, "-p", str(req.port), req.server_host]
        out, rc, err = run_cmd(cmd, timeout=req.duration + 60)
        return {"role": "client", "tool": req.tool, "rc": rc, "output": out,
                "error": err}
    if req.tool == "ping":
        out, rc, err = run_cmd(
            ["ping", "-c", str(req.count), req.server_host], timeout=req.count * 3 + 10
        )
        return {"role": "client", "tool": req.tool, "rc": rc, "output": out,
                "error": err}
    raise HTTPException(400, f"未知 tool: {req.tool}")


# ---------------------------------------------------------------------------
# 模型管理（接收 / Agent 高速直传 / 列表 / 删除）
#   - head 经管理网从控制平面回拉；
#   - worker 通过短期令牌从 head 高速地址并发回拉，不依赖 SSH。
# ---------------------------------------------------------------------------

DEFAULT_HF_CACHE = Path.home() / ".cache" / "huggingface"
_model_jobs: dict[str, dict] = {}  # job_id -> {kind, status, ...}


def hf_cache_dir(cache_dir: str | None = None) -> Path:
    return Path(cache_dir) if cache_dir else DEFAULT_HF_CACHE


def _model_dir(repo: str, cache_dir: str | None = None) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "--", repo)
    return hf_cache_dir(cache_dir) / "hub" / f"models--{safe}"


def _dir_size(p: Path) -> int:
    """HF hub 缓存真实内容大小（唯一字节数）。

    - 只统计真实文件（跳过 symlink，避免 snapshots/ 链接重复计数）
    - 排除 *.incomplete / *.lock 下载临时文件（与 rsync 排除规则一致）
    """
    if not p.exists():
        return 0
    total = 0
    for f in p.rglob("*"):
        if f.is_symlink() or not f.is_file():
            continue
        if f.name.endswith((".incomplete", ".lock")):
            continue
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _snapshot_id(repo: str, cache_dir: str | None = None) -> str | None:
    snap = _model_dir(repo, cache_dir) / "snapshots"
    if not snap.exists():
        return None
    entries = [d for d in snap.iterdir() if d.is_dir()]
    return entries[0].name if entries else None


class ModelPullRequest(BaseModel):
    repo: str
    relpath: str
    url: str          # 控制平面文件相对路径
    size: int = 0
    symlink: str | None = None
    hash_algo: str | None = None
    digest: str | None = None
    transfer_id: int = Field(ge=1)


def _resolve_pull_url(client_ip: str, url: str) -> str:
    """回拉地址解析：后端下发的相对路径 → 用「下发请求的来源 IP」补全控制端地址。

    控制平面 -> Agent 的请求来自控制平面管理网出口 IP（docker 部署经宿主机 NAT，
    节点看到的正是宿主机管理网 IP），Agent 据此回拉恰好可达——控制端换机/换 IP
    后无需任何配置。控制平面 API 端口为部署约定（8000）。

    仅接受相对路径，绝对 URL 一律拒绝；下载不跟随重定向（见
    _open_control_plane），避免 Agent 被诱导访问任意内网地址。
    """
    if url.startswith(("http://", "https://")):
        raise HTTPException(400, "回拉 URL 必须使用控制平面相对路径")
    # 相对路径：统一补前导 /，按来源 IP 拼写（后端始终下发 /api/... 路径）
    path = url if url.startswith("/") else "/" + url
    if not client_ip:
        raise HTTPException(400, "无法推断控制平面地址（缺少来源 IP）")
    return f"http://{client_ip}:8000{path}"


def _validate_pull_symlink(model_dir: Path, target: Path, symlink: str) -> None:
    """校验 symlink 目标：禁止绝对路径；解析后必须落在模型目录内。

    HF 标准布局 snapshots/<commit>/<file> -> ../../blobs/<sha>（相对路径）允许；
    绝对路径（如 /etc/passwd、远程挂载点）与越出模型目录的引用一律拒绝。
    """
    if symlink.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", symlink):
        raise HTTPException(400, "非法 symlink 目标（禁止绝对路径）")
    base = Path(os.path.abspath(model_dir))
    resolved = (target.parent / symlink).resolve()
    if str(resolved) != str(base) and not str(resolved).startswith(str(base) + os.sep):
        raise HTTPException(400, "非法 symlink 目标（越出模型目录）")


def _git_blob_sha1(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(b"blob %d\0" % size)
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_file_matches(path: Path, size: int, hash_algo: str | None,
                        digest: str | None) -> bool:
    """按 manifest 校验模型文件；拒绝仅凭相同大小复用旧文件。"""
    if path.is_symlink() or not path.is_file() or (size and path.stat().st_size != size):
        return False
    if not digest:
        return False
    if hash_algo == "sha256":
        actual = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                actual.update(chunk)
        return hmac.compare_digest(actual.hexdigest(), digest)
    if hash_algo == "git-sha1":
        return hmac.compare_digest(_git_blob_sha1(path), digest)
    raise HTTPException(400, f"不支持的模型文件哈希算法: {hash_algo}")


@app.post("/api/model/pull")
def model_pull(req: ModelPullRequest, request: Request):
    """从控制平面（管理网）拉取模型文件（GET 流式下载，断点续传）。

    中断保留 .part 分片（不删除），重试带 Range: bytes=N- 从断点续传；
    完成后按总大小校验并 rename。控制平面 FileResponse 支持 Range（206）。
    """
    if req.symlink is None and (
        req.hash_algo not in ("sha256", "git-sha1") or not req.digest
    ):
        raise HTTPException(400, "普通模型文件必须携带内容摘要")
    pull_url = _resolve_pull_url(request.client.host if request.client else "", req.url)
    d = _model_dir(req.repo)
    d.mkdir(parents=True, exist_ok=True)
    # 同 model_receive：abspath 规范化，不跟随 symlink（防二次传输误删真实 blobs）
    target = Path(os.path.abspath(d / req.relpath))
    if not str(target).startswith(str(os.path.abspath(d)) + os.sep):
        raise HTTPException(400, "非法 relpath")
    if req.symlink:
        target.parent.mkdir(parents=True, exist_ok=True)
        _validate_pull_symlink(d, target, req.symlink)
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(req.symlink)
        return {"ok": True, "symlink": req.symlink, "relpath": req.relpath}
    if target.exists() and _model_file_matches(
        target, req.size, req.hash_algo, req.digest,
    ):
        notify_progress(
            "model", f"{req.transfer_id}:{req.relpath}", req.size, req.size,
        )
        return {"ok": True, "relpath": req.relpath, "skipped": True}
    if target.exists() or target.is_symlink():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0
    if have >= req.size:
        # 残留分片已完整/超长：删除重下
        tmp.unlink(missing_ok=True)
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with _open_control_plane(pull_url, headers, 600) as resp, \
            open(tmp, "ab" if have else "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            have += len(chunk)
            notify_progress(
                "model", f"{req.transfer_id}:{req.relpath}", have, req.size,
            )
    total = tmp.stat().st_size
    if req.size and total != req.size:
        raise HTTPException(400, f"大小不匹配: {total} != {req.size}")
    if not _model_file_matches(tmp, req.size, req.hash_algo, req.digest):
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"文件哈希校验失败: {req.relpath}")
    tmp.rename(target)
    return {"ok": True, "relpath": req.relpath, "bytes": total}


@app.get("/api/model/list")
def model_list(cache_dir: str | None = None):
    """本机已接收模型列表（repo + 大小 + 快照）。"""
    base = hf_cache_dir(cache_dir) / "hub"
    models = []
    if base.exists():
        for d in sorted(base.glob("models--*")):
            if not d.is_dir():
                continue
            repo = d.name[len("models--"):].replace("--", "/")
            models.append({
                "repo": repo,
                "size_bytes": _dir_size(d),
                "snapshot": _snapshot_id(repo, cache_dir),
            })
    return {"models": models}


def _verify_model(repo: str, cache_dir: str | None = None) -> dict:
    """逐文件校验节点缓存：trees 元数据 vs snapshots symlink 目标 blobs 大小。

    与控制平面校验逻辑一致：trees 为新版 hub 格式 {rfilename: {size, blob_id, ...}}
    （commit 取文件名），每个文件对应 symlink 存在且 blobs 大小匹配才算完整。
    """
    d = _model_dir(repo, cache_dir)
    trees = list((d / "trees").glob("*.json"))
    if not trees:
        return {"ok": False, "error": "缺少 trees 元数据（模型未完整下载）"}
    data = None
    sha = None
    for t in trees:
        try:
            cand = json.loads(t.read_text())
        except Exception:  # noqa: BLE001
            continue
        entries = cand if isinstance(cand, dict) else {}
        if any(isinstance(v, dict) and "size" in v for v in entries.values()):
            data = entries
            sha = t.stem
            break
    if data is None:
        return {"ok": False, "error": "trees 清单无效/为空（模型未完整下载）"}
    snap = d / "snapshots" / sha
    missing: list[str] = []
    for rel, info in data.items():
        size = info.get("size") or 0
        link = snap / rel
        if not (link.is_symlink() and link.exists()):
            missing.append(rel)
            continue
        blob = link.resolve()
        if not blob.is_file() or blob.stat().st_size != size:
            missing.append(rel)
    if missing:
        return {"ok": False, "missing": missing[:20],
                "error": f"完整性校验失败：{len(missing)} 个文件缺失/不完整"}
    return {"ok": True, "missing": [], "error": None}


@app.get("/api/model/cache/{repo:path}")
def model_cache_repo(repo: str, cache_dir: str | None = None):
    """指定模型缓存状态（存在性/大小/快照/完整性）。

    complete：逐文件校验通过（trees 元数据 vs blobs 大小）。
    部分下载/中断/布局残留均判定为不完整，不会误报"已缓存"。
    """
    d = _model_dir(repo, cache_dir)
    v = _verify_model(repo, cache_dir)
    return {
        "repo": repo,
        "cached": d.exists(),
        "complete": v["ok"],
        "size_bytes": _dir_size(d),
        "snapshot": _snapshot_id(repo, cache_dir),
        "verify_error": v.get("error"),
    }


@app.delete("/api/model/{repo:path}")
def model_delete(repo: str, cache_dir: str | None = None):
    """删除本机上的指定模型缓存。"""
    d = _model_dir(repo, cache_dir)
    if d.exists():
        shutil.rmtree(d)
        return {"ok": True, "repo": repo, "deleted": True}
    return {"ok": True, "repo": repo, "deleted": False}


# ---------- 模型 Agent 间高速直传（manifest + 文件流，无 SSH） ----------

_model_shares: dict[str, dict] = {}
_model_shares_lock = threading.Lock()
_model_fetch_cancel: dict[str, threading.Event] = {}


def _model_entry(path: Path, relpath: Path) -> dict:
    if path.is_symlink():
        return {"relpath": str(relpath), "type": "symlink", "target": os.readlink(path)}
    size = path.stat().st_size
    # HF blobs 已以内容摘要命名，避免为数百 GB 权重重复扫描；refs/trees 等小文件
    # 计算 SHA-256，保证整个缓存布局均可验证。
    if relpath.parts and relpath.parts[0] == "blobs" and re.fullmatch(
        r"[0-9a-f]{64}", path.name,
    ):
        algo, digest = "sha256", path.name
    elif relpath.parts and relpath.parts[0] == "blobs" and re.fullmatch(
        r"[0-9a-f]{40}", path.name,
    ):
        algo, digest = "git-sha1", path.name
    else:
        algo = "sha256"
        h = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
    return {
        "relpath": str(relpath), "type": "file", "size": size,
        "hash_algo": algo, "digest": digest,
    }


def _model_manifest(repo: str, cache_dir: str | None = None) -> tuple[list[dict], int]:
    source = _model_dir(repo, cache_dir)
    if not source.exists():
        raise HTTPException(404, f"模型缓存不存在: {repo}")
    entries: list[dict] = []
    total = 0
    for path in sorted(source.rglob("*")):
        relpath = path.relative_to(source)
        if path.name.endswith((".part", ".incomplete", ".lock")) or ".part." in path.name:
            continue
        if path.is_symlink() or path.is_file():
            entry = _model_entry(path, relpath)
            entries.append(entry)
            total += int(entry.get("size") or 0)
    if not entries:
        raise HTTPException(409, f"模型缓存为空: {repo}")
    return entries, total


class ModelShareRequest(BaseModel):
    repo: str
    ttl: int = Field(default=21600, ge=60, le=86400)
    cache_dir: str | None = None


@app.post("/api/model/share")
def model_share(req: ModelShareRequest):
    """签发绑定单个模型仓库的短期只读共享令牌。"""
    manifest, total = _model_manifest(req.repo, req.cache_dir)
    share_id = uuid.uuid4().hex
    token = uuid.uuid4().hex + uuid.uuid4().hex
    now = time.time()
    with _model_shares_lock:
        for old_token, info in list(_model_shares.items()):
            if info.get("expires", 0) < now:
                _model_shares.pop(old_token, None)
        _model_shares[token] = {
            "share_id": share_id, "repo": req.repo, "cache_dir": req.cache_dir,
            "expires": now + req.ttl,
        }
    return {
        "token": token, "path": f"/api/model/share/{share_id}",
        "manifest": manifest, "total_size": total, "expires_at": now + req.ttl,
    }


def _shared_model_info(share_id: str, token: str) -> dict:
    now = time.time()
    with _model_shares_lock:
        for old_token, info in list(_model_shares.items()):
            if info.get("expires", 0) < now:
                _model_shares.pop(old_token, None)
        info = _model_shares.get(token)
    if not info or info.get("share_id") != share_id:
        raise HTTPException(401, "模型传输令牌无效或已过期")
    return info


@app.get("/api/model/share/{share_id}")
def model_share_file(share_id: str, relpath: str, request: Request):
    """通过短期令牌读取模型中的单个普通文件，FileResponse 原生支持 Range。"""
    info = _shared_model_info(share_id, request.headers.get("X-Transfer-Token", ""))
    source = _model_dir(info["repo"], info.get("cache_dir"))
    target = Path(os.path.abspath(source / relpath))
    base = Path(os.path.abspath(source))
    if str(target) == str(base) or not str(target).startswith(str(base) + os.sep):
        raise HTTPException(400, "非法模型相对路径")
    if target.is_symlink() or not target.is_file():
        raise HTTPException(404, "模型文件不存在")
    response = FileResponse(target, media_type="application/octet-stream")
    response.chunk_size = 4 << 20
    return response


class ModelFetchRequest(BaseModel):
    source_url: str
    source_token: str
    repo: str
    manifest: list[dict]
    total_size: int = 0
    transfer_id: int
    connections: int = Field(default=4, ge=1, le=8)


def _validate_model_source_url(source_url: str) -> None:
    from ipaddress import ip_address
    from urllib.parse import urlsplit

    parts = urlsplit(source_url)
    try:
        host = ip_address(parts.hostname or "")
    except ValueError as exc:
        raise HTTPException(400, "模型源地址必须是 IP") from exc
    if parts.scheme != "http" or not (host.is_private or host.is_loopback or host.is_link_local):
        raise HTTPException(400, "模型源地址必须是私有高速网络 HTTP 地址")
    if not parts.path.startswith("/api/model/share/"):
        raise HTTPException(400, "非法模型共享路径")


def _safe_model_path(model_dir: Path, relpath: str) -> Path:
    target = Path(os.path.abspath(model_dir / relpath))
    base = Path(os.path.abspath(model_dir))
    if str(target) == str(base) or not str(target).startswith(str(base) + os.sep):
        raise RuntimeError(f"非法模型相对路径: {relpath}")
    return target


def _download_shared_model_file(req: ModelFetchRequest, entry: dict, job: dict,
                                lock: threading.Lock, cancel: threading.Event) -> int:
    import urllib.error
    import urllib.parse
    import urllib.request

    relpath = str(entry.get("relpath") or "")
    size = int(entry.get("size") or 0)
    target = _safe_model_path(_model_dir(req.repo), relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    algo, digest = entry.get("hash_algo"), entry.get("digest")
    if target.exists() and _model_file_matches(target, size, algo, digest):
        with lock:
            job["file_bytes"][relpath] = size
            job["transferred_bytes"] = sum(job["file_bytes"].values())
            notify_progress("model-sync", str(req.transfer_id),
                            job["transferred_bytes"], req.total_size)
        return size
    if target.exists() or target.is_symlink():
        target.unlink()
    tmp = target.with_name(target.name + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0
    if size and have >= size:
        tmp.unlink(missing_ok=True)
        have = 0
    url = req.source_url + "?" + urllib.parse.urlencode({"relpath": relpath})
    attempts = 0
    while True:
        if cancel.is_set():
            raise RuntimeError("传输已取消")
        attempts += 1
        headers = {"X-Transfer-Token": req.source_token}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with _no_redirect_opener().open(
                urllib.request.Request(url, headers=headers), timeout=60,
            ) as response:
                if have and getattr(response, "status", 200) == 200:
                    tmp.unlink(missing_ok=True)
                    have = 0
                    continue
                with open(tmp, "ab" if have else "wb") as stream:
                    while True:
                        if cancel.is_set():
                            raise RuntimeError("传输已取消")
                        chunk = response.read(4 << 20)
                        if not chunk:
                            break
                        stream.write(chunk)
                        have += len(chunk)
                        with lock:
                            job["file_bytes"][relpath] = have
                            job["transferred_bytes"] = sum(job["file_bytes"].values())
                            job["current_file"] = relpath
                            notify_progress("model-sync", str(req.transfer_id),
                                            job["transferred_bytes"], req.total_size)
            if size and have != size:
                if attempts >= 3:
                    raise RuntimeError(f"模型文件大小不符 {relpath}: {have} != {size}")
                tmp.unlink(missing_ok=True)
                have = 0
                time.sleep(2 ** attempts)
                continue
            if not _model_file_matches(tmp, size, algo, digest):
                tmp.unlink(missing_ok=True)
                have = 0
                if attempts >= 3:
                    raise RuntimeError(f"模型文件哈希校验失败: {relpath}")
                time.sleep(2 ** attempts)
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempts >= 3 or cancel.is_set():
                raise
            time.sleep(2 ** attempts)
    tmp.replace(target)
    return have


def _do_model_fetch(job_id: str, req: ModelFetchRequest) -> None:
    job = _model_jobs[job_id]
    cancel = _model_fetch_cancel[job_id]
    lock = threading.Lock()
    try:
        model_dir = _model_dir(req.repo)
        model_dir.mkdir(parents=True, exist_ok=True)
        files = [entry for entry in req.manifest if entry.get("type") == "file"]
        symlinks = [entry for entry in req.manifest if entry.get("type") == "symlink"]
        job["file_bytes"] = {}
        with ThreadPoolExecutor(max_workers=req.connections) as pool:
            futures = [
                pool.submit(_download_shared_model_file, req, entry, job, lock, cancel)
                for entry in files
            ]
            for future in as_completed(futures):
                future.result()
        if cancel.is_set():
            raise RuntimeError("传输已取消")
        for entry in symlinks:
            target = _safe_model_path(model_dir, str(entry.get("relpath") or ""))
            target.parent.mkdir(parents=True, exist_ok=True)
            link = str(entry.get("target") or "")
            _validate_pull_symlink(model_dir, target, link)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(link)
        job.update(
            status="completed", transferred_bytes=req.total_size,
            current_file=None, finished=time.time(),
        )
        notify_progress("model-sync", str(req.transfer_id), req.total_size, req.total_size)
    except Exception as exc:  # noqa: BLE001
        job.update(
            status="cancelled" if cancel.is_set() else "failed",
            error="用户取消" if cancel.is_set() else str(exc),
            finished=time.time(),
        )
    finally:
        job.pop("file_bytes", None)


@app.post("/api/model/fetch")
def model_fetch(req: ModelFetchRequest):
    """worker 后台并发直拉 head 模型文件；支持进度、取消和 .part 续传。"""
    _validate_model_source_url(req.source_url)
    job_id = uuid.uuid4().hex[:12]
    _model_jobs[job_id] = {
        "kind": "peer-fetch", "status": "running", "repo": req.repo,
        "transfer_id": req.transfer_id, "transferred_bytes": 0,
        "total_bytes": req.total_size, "current_file": None,
        "started": time.time(), "error": None,
    }
    _model_fetch_cancel[job_id] = threading.Event()
    threading.Thread(target=_do_model_fetch, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/model/fetch/{job_id}")
def model_fetch_status(job_id: str):
    removed = _prune_jobs(_model_jobs)
    for removed_job_id in removed:
        _model_fetch_cancel.pop(removed_job_id, None)
    job = _model_jobs.get(job_id)
    if not job or job.get("kind") != "peer-fetch":
        raise HTTPException(404, "任务不存在")
    return {key: value for key, value in job.items() if key != "file_bytes"}


@app.post("/api/model/fetch/{job_id}/cancel")
def model_fetch_cancel(job_id: str):
    job = _model_jobs.get(job_id)
    cancel = _model_fetch_cancel.get(job_id)
    if not job or job.get("kind") != "peer-fetch" or cancel is None:
        raise HTTPException(404, "任务不存在")
    if job.get("status") == "running":
        cancel.set()
        job["status"] = "cancelling"
    return {"ok": True, "status": job.get("status")}


# ---------- 镜像分发（控制平面回拉 -> Agent 间高速 HTTP 直传 -> docker load） ----------

IMAGE_DIR = Path.home() / ".fireworks-images"

# 传输进度监听器（WS 推送用；无订阅者时 no-op）
_progress_listeners: list = []


def notify_progress(kind: str, key: str, written: int, total: int) -> None:
    """上报传输进度（kind: model|image；key: 文件/归档标识；total=0 表示未知）。"""
    for fn in list(_progress_listeners):
        try:
            fn(kind, key, written, total)
        except Exception:  # noqa: BLE001
            pass


class ImagePullRequest(BaseModel):
    image: str
    digest: str = ""
    url: str          # 控制平面归档相对路径
    size: int = 0


def _validate_archive_digest(digest: str) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""):
        raise HTTPException(400, "digest 必须是完整 sha256 指纹")


def _file_fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _verification_marker(target: Path) -> Path:
    return target.with_name(f".{target.name}.verified")


def _mark_archive_verified(target: Path, digest: str) -> None:
    stat = target.stat()
    _verification_marker(target).write_text(
        json.dumps({
            "digest": digest, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        }),
        encoding="utf-8",
    )


def _archive_has_valid_marker(target: Path, digest: str) -> bool:
    try:
        stat = target.stat()
        info = json.loads(_verification_marker(target).read_text(encoding="utf-8"))
        return (
            info.get("digest") == digest
            and info.get("size") == stat.st_size
            and info.get("mtime_ns") == stat.st_mtime_ns
        )
    except (OSError, ValueError, TypeError):
        return False


def _image_present(image: str) -> bool:
    """docker 中是否真实存在该镜像 tag（docker 全局存储，跨连接可靠）。"""
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=30,
        )
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _download_image_archive(
    source_url: str,
    headers: dict[str, str],
    target: Path,
    digest: str,
    expected_size: int,
    progress_kind: str,
    progress_key: str,
) -> dict:
    """流式下载归档，支持 Range 断点续传并以大小 + SHA-256 收尾校验。

    任意中断（断流/超时/连接重置/提前 EOF）都**保留** .part 分片，重试时带
    Range: bytes=N- 从断点继续；只有以下两种情形才删除分片从头重下：
    - 服务端忽略 Range（返回 200，append 会造成归档重复拼接）；
    - 已完成下载但内容校验失败（分片确已损坏）。
    重试有界（6 次），避免网络抖动导致整个分发任务反复失败。
    """
    import http.client
    import urllib.error
    import urllib.request

    if target.exists():
        size_ok = not expected_size or target.stat().st_size == expected_size
        # 已存在且完整的归档直接跳过：优先复用验证标记（免整份重读），
        # 标记缺失/失效（如升级前的旧 Agent / 文件被替换）才回退全量 sha256。
        if size_ok and (
            _archive_has_valid_marker(target, digest)
            or _file_fingerprint(target) == digest
        ):
            _mark_archive_verified(target, digest)
            notify_progress(progress_kind, progress_key, target.stat().st_size,
                            expected_size or target.stat().st_size)
            return {"ok": True, "skipped": True, "bytes": target.stat().st_size}
        target.unlink(missing_ok=True)
        _verification_marker(target).unlink(missing_ok=True)

    tmp = target.with_name(target.name + ".part")
    attempts = 0
    while True:
        have = tmp.stat().st_size if tmp.exists() else 0
        if expected_size and have >= expected_size:
            # 残留分片已完整/超长：删除重下（超长几乎必为拼接损坏）
            tmp.unlink(missing_ok=True)
            have = 0
        request_headers = dict(headers)
        if have:
            request_headers["Range"] = f"bytes={have}-"
        last_report = 0.0
        try:
            with _no_redirect_opener().open(
                urllib.request.Request(source_url, headers=request_headers),
                timeout=60,
            ) as resp:
                if have and getattr(resp, "status", 200) == 200:
                    # 服务端忽略 Range：从头重下（不能 append，否则重复拼接）
                    tmp.unlink(missing_ok=True)
                    have = 0
                with open(tmp, "ab" if have else "wb") as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= 0.5:
                            notify_progress(progress_kind, progress_key, have,
                                            expected_size or have)
                            last_report = now
        except urllib.error.HTTPError as exc:
            if exc.code != 416:
                raise  # 4xx/5xx 明确错误不重试（416 = Range 越界，单独处理）
            # Range 越界：.part 已完整（或服务器端文件已变化/不完整）。
            # 完整且校验通过则收尾，否则删除从头重下，避免 416 死循环。
            if (not expected_size or have == expected_size) and have > 0 \
                    and _file_fingerprint(tmp) == digest:
                tmp.replace(target)
                _mark_archive_verified(target, digest)
                notify_progress(progress_kind, progress_key, have,
                                expected_size or have)
                return {"ok": True, "bytes": have, "path": str(target)}
            tmp.unlink(missing_ok=True)
            have = 0
            attempts += 1
            if attempts >= 6:
                raise RuntimeError(
                    "归档持续返回 416（服务器端文件不完整或已变化）")
            time.sleep(1)
            continue
        except (http.client.IncompleteRead, urllib.error.URLError,
                TimeoutError, OSError):
            # 断流/超时：保留 .part，下一轮带 Range 从断点续传
            attempts += 1
            if attempts >= 6:
                raise
            time.sleep(min(2 ** (attempts - 1), 15))
            continue
        if expected_size and have != expected_size:
            # 主体提前结束（截断）：保留 .part 续传，不删除
            attempts += 1
            if attempts >= 6:
                raise RuntimeError(
                    f"归档大小不符: {have} != {expected_size}（重试 6 次仍截断）")
            time.sleep(min(2 ** (attempts - 1), 15))
            continue
        if _file_fingerprint(tmp) != digest:
            # 收尾校验失败：损坏分片，删除从头重下
            tmp.unlink(missing_ok=True)
            attempts += 1
            if attempts >= 6:
                raise RuntimeError("归档 SHA-256 校验失败，已删除损坏分片")
            time.sleep(1)
            continue
        tmp.replace(target)
        _mark_archive_verified(target, digest)
        notify_progress(progress_kind, progress_key, have, expected_size or have)
        return {"ok": True, "bytes": have, "path": str(target)}


@app.post("/api/image/pull")
def image_pull(req: ImagePullRequest, request: Request):
    """从控制平面拉取镜像归档（GET 流式，断点续传：中断保留 .part，重试带 Range 续传）。"""
    _validate_archive_digest(req.digest)
    pull_url = _resolve_pull_url(
        request.client.host if request.client else "", req.url
    )
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    target = IMAGE_DIR / f"{req.digest}.tar"
    return _download_image_archive(
        pull_url,
        {"Authorization": f"Bearer {AGENT_TOKEN}"},
        target,
        req.digest,
        req.size,
        "image",
        req.digest,
    )


_image_shares: dict[str, dict] = {}
_image_shares_lock = threading.Lock()


def _prune_image_shares(now: float) -> None:
    for token, info in list(_image_shares.items()):
        if info.get("expires", 0) < now:
            _image_shares.pop(token, None)


class ImageShareRequest(BaseModel):
    digest: str
    ttl: int = Field(default=7200, ge=60, le=86400)


@app.post("/api/image/share")
def image_share(req: ImageShareRequest):
    """为单个归档签发短期只读令牌，供 worker 通过高速网回拉。"""
    _validate_archive_digest(req.digest)
    target = IMAGE_DIR / f"{req.digest}.tar"
    if not target.exists():
        raise HTTPException(404, "镜像归档不存在")
    if not _archive_has_valid_marker(target, req.digest) and _file_fingerprint(target) != req.digest:
        raise HTTPException(409, "镜像归档校验失败")
    _mark_archive_verified(target, req.digest)
    token = uuid.uuid4().hex + uuid.uuid4().hex
    with _image_shares_lock:
        _prune_image_shares(time.time())
        _image_shares[token] = {
            "digest": req.digest,
            "expires": time.time() + req.ttl,
        }
    return {
        "token": token,
        "path": f"/api/image/share/{req.digest}",
        "size": target.stat().st_size,
        "expires_at": _image_shares[token]["expires"],
    }


@app.get("/api/image/share/{digest}")
def image_share_file(digest: str, request: Request):
    """短期令牌保护的归档流；不接受节点长期管理 token。"""
    _validate_archive_digest(digest)
    now = time.time()
    token = request.headers.get("X-Transfer-Token", "")
    with _image_shares_lock:
        _prune_image_shares(now)
        info = _image_shares.get(token)
    if not info or info.get("digest") != digest:
        raise HTTPException(401, "传输令牌无效或已过期")
    target = IMAGE_DIR / f"{digest}.tar"
    if not target.exists():
        raise HTTPException(404, "镜像归档不存在")
    response = FileResponse(target, media_type="application/octet-stream")
    response.chunk_size = 4 << 20
    return response


class ImageFetchRequest(BaseModel):
    source_url: str
    source_token: str
    image: str
    digest: str
    size: int = 0
    transfer_id: int


@app.post("/api/image/fetch")
def image_fetch(req: ImageFetchRequest):
    """worker 从 head Agent 高速网地址直拉归档，不依赖 SSH/rsync。"""
    from ipaddress import ip_address
    from urllib.parse import urlsplit

    _validate_archive_digest(req.digest)
    parts = urlsplit(req.source_url)
    try:
        host = ip_address(parts.hostname or "")
    except ValueError as exc:
        raise HTTPException(400, "源地址必须是 IP") from exc
    if parts.scheme != "http" or not (host.is_private or host.is_loopback or host.is_link_local):
        raise HTTPException(400, "源地址必须是私有高速网络 HTTP 地址")
    if not parts.path.startswith("/api/image/share/"):
        raise HTTPException(400, "非法镜像共享路径")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    return _download_image_archive(
        req.source_url,
        {"X-Transfer-Token": req.source_token},
        IMAGE_DIR / f"{req.digest}.tar",
        req.digest,
        req.size,
        "image-sync",
        str(req.transfer_id),
    )


class ImageLoadRequest(BaseModel):
    image: str
    digest: str = ""


@app.post("/api/image/load")
def image_load(req: ImageLoadRequest):
    """docker load 镜像归档并校验 digest（归档文件 sha256 指纹）。

    digest 为归档文件指纹（构建确定性，跨节点字节一致）。跳过条件：
    本节点已用该归档指纹 load 成功过（标记文件 .loaded-<digest>，
    比 docker RepoDigests 可靠——load 镜像无 registry digest，且避免
    已更新的同名镜像误判为已加载）。load 成功后写入标记。
    """
    _validate_archive_digest(req.digest)
    target = IMAGE_DIR / f"{req.digest}.tar"
    if not target.exists():
        return {"ok": False, "error": f"归档不存在: {target}"}
    # 归档 sha256 指纹校验（下载/同步完整性）
    h = hashlib.sha256()
    with open(target, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if f"sha256:{h.hexdigest()}" != req.digest:
        return {"ok": False, "error": "归档指纹校验失败（文件损坏）"}
    # 该指纹已 load 成功过 且 docker 中镜像仍真实存在 -> 跳过（幂等，不重复加载）。
    # 仅凭 .loaded-<digest> 标记会在镜像被 docker 清理/更换后残留，造成「假完成」：
    # 任务成功但节点上没有可用镜像（标记在、镜像不在）。
    mark = IMAGE_DIR / f".loaded-{req.digest}"
    if mark.exists() and _image_present(req.image):
        return {"ok": True, "skipped": True}
    # docker load（流式喂 stdin）
    try:
        with open(target, "rb") as f:
            proc = subprocess.run(["docker", "load"], stdin=f,
                                  capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            return {"ok": False, "error": f"docker load 失败: {proc.stderr.strip()[:300]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"docker load 异常: {e}"}
    # load 后校验镜像存在
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", req.image, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return {"ok": False, "error": "docker load 后镜像不存在"}
    except Exception:  # noqa: BLE001
        pass
    mark.touch()
    return {"ok": True, "loaded": True}


def _prune_jobs(jobs: dict, ttl: float = 3600) -> list[str]:
    """清理已完成/失败超过 TTL 的同步任务记录（防内存无界增长）。"""
    now = time.time()
    stale = [
        k for k, j in jobs.items()
        if j.get("status") in ("completed", "failed", "cancelled")
        and now - j.get("finished", j.get("started", 0)) > ttl
    ]
    for k in stale:
        jobs.pop(k, None)
    return stale


@app.get("/api/image/status")
def image_status(image: str):
    """节点上指定镜像状态。

    - present: docker 中存在该 tag（展示用/发布前就绪判定用）
    就绪更精确的版本一致性由镜像传输流程保证（monitor 阶段 4 的 image_load
    会按归档指纹 .loaded-<digest> 标记加载，无标记即 load 新版本）。
    """
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=30,
        )
        return {
            "image": image,
            "present": out.returncode == 0 and bool(out.stdout.strip()),
        }
    except Exception as e:  # noqa: BLE001
        return {"image": image, "present": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 通用子进程 / 控制平面回拉工具
# ---------------------------------------------------------------------------


def _terminate_proc(p) -> None:
    """终止子进程并回收（wait），避免长驻 agent 进程下残留僵尸。"""
    try:
        if p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)
    except Exception:  # noqa: BLE001 - 进程已消失等，忽略
        pass


def _no_redirect_opener():
    """不跟随重定向的 urlopen opener：回拉请求禁止被跳转到内网/其他主机。"""
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
            return None

    return urllib.request.build_opener(_NoRedirect)


def _open_control_plane(pull_url: str, headers: dict, timeout: int):
    """带共享 token（Authorization 头）打开控制平面回拉 URL，不跟随重定向。"""
    import urllib.request

    headers = dict(headers)
    headers["Authorization"] = f"Bearer {AGENT_TOKEN}"
    req = urllib.request.Request(pull_url, headers=headers)
    return _no_redirect_opener().open(req, timeout=timeout)


# 网络测试 server 进程登记 ((tool, port) -> Popen)，供 stop/换端口时回收，避免僵尸
_test_servers: dict[tuple[str, int], subprocess.Popen] = {}


def _reap_test_server(tool: str, port: int) -> None:
    """终止并回收已登记的测试 server 进程。"""
    p = _test_servers.pop((tool, port), None)
    if p:
        _terminate_proc(p)


# ---------------------------------------------------------------------------
# WebSocket 事件推送（后端实时订阅：指标 / 容器事件 / 日志流 / 传输进度）
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

WS_METRICS_INTERVAL = float(os.environ.get("FW_WS_METRICS_INTERVAL", "5"))


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    """后端实时数据通道。

    推送：metrics（每 5s）、docker_event（容器生命周期）、log（docker logs -f 行）、
    progress（模型/镜像拉取进度）、log_end（日志流结束）。
    接收：log_subscribe {container, tail, generation} /
    log_unsubscribe {container, generation}。
    """
    await websocket.accept()
    # 握手鉴权：先 accept 再校验，无效立即以 4401 关闭（客户端收到正常 WS 关闭码）
    candidate = _token_from_headers(websocket.headers) or (websocket.query_params.get("token") or "")
    if not _valid_token(candidate):
        await websocket.close(code=4401)
        return
    loop = asyncio.get_running_loop()
    send_q: asyncio.Queue = asyncio.Queue(maxsize=2000)  # 背压：慢消费者丢弃旧指标
    stop = asyncio.Event()
    log_procs: dict[str, tuple[subprocess.Popen, int]] = {}
    log_tasks: dict[str, asyncio.Task] = {}

    def _enqueue(msg: dict) -> None:
        """事件循环线程内执行入队：队满丢弃最旧一条（与消费循环互斥）。"""
        try:
            if send_q.full():
                try:
                    send_q.get_nowait()
                except asyncio.QueueEmpty:  # 竞态：消费循环正好取走
                    pass
            send_q.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # 极端竞争，放弃本条

    def push(msg: dict) -> None:
        """线程安全推送（进度回调来自同步线程池）。

        asyncio.Queue 非线程安全：跨线程直接 put_nowait 会与事件循环内
        get_nowait 并发破坏队列（大文件传输时进度回调频率高）。统一经
        loop.call_soon_threadsafe 把入队调度回事件循环线程执行——回调内无
        await，天然与消费循环互斥。
        """
        try:
            loop.call_soon_threadsafe(_enqueue, msg)
        except RuntimeError:  # 事件循环已关闭（WS 清理中）
            pass

    def on_progress(kind: str, key: str, written: int, total: int) -> None:
        push({"type": "progress", "kind": kind, "key": key,
              "written": written, "total": total})

    # 握手后立即发送 hello：控制平面心跳计时从握手开始，并携带 agent 版本供识别
    push({"type": "hello", "version": APP_VERSION, "time": time.time()})

    _progress_listeners.append(on_progress)

    async def metrics_loop():
        while not stop.is_set():
            try:
                # collect_metrics 含子进程调用（nvidia-smi 等），放线程池避免阻塞事件循环
                data = await asyncio.to_thread(collect_metrics)
                push({"type": "metrics", "data": data})
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=WS_METRICS_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def docker_events_loop():
        proc = subprocess.Popen(
            ["docker", "events", "--format", "{{json .}}", "--filter", "type=container"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            while not stop.is_set():
                # readline 是同步阻塞：docker events 空闲时无输出会永久阻塞，
                # 必须放线程池，否则卡死整个 WS 事件循环
                line = await asyncio.to_thread(proc.stdout.readline)
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                push({"type": "docker_event", "data": ev})
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    async def log_reader(container: str, proc: subprocess.Popen, generation: int):
        buf = b""
        prev_update = False
        try:
            while not stop.is_set():
                # read1：只读当前可用缓冲立即返回（read(65536) 会阻塞等满 64KB，
                # 实时日志将长期卡在管道缓冲里不推送）
                chunk = await asyncio.to_thread(proc.stdout.read1, 1 << 16)
                if not chunk:
                    break
                segments, buf, prev_update = _log_segments(chunk, buf, prev_update)
                for seg, update in segments:
                    push({"type": "log", "container": container,
                          "line": seg.decode("utf-8", errors="replace").rstrip("\n"),
                          "update": update, "generation": generation})
                if len(buf) > (1 << 20):  # 无分隔符巨型段防堆积：按完整行追加
                    push({"type": "log", "container": container,
                          "line": buf.decode("utf-8", errors="replace"),
                          "generation": generation})
                    buf = b""
                    prev_update = False
        finally:
            if buf:
                push({"type": "log", "container": container,
                      "line": buf.decode("utf-8", errors="replace"),
                      "generation": generation})
            push({"type": "log_end", "container": container,
                  "generation": generation})
            current = log_procs.get(container)
            if current and current[0] is proc:
                log_procs.pop(container, None)
                log_tasks.pop(container, None)

    def start_log_stream(container: str, tail: int, generation: int):
        current = log_procs.get(container)
        if current and current[1] == generation:
            return
        if current:
            stop_log_stream(container, current[1])
        p = subprocess.Popen(
            ["docker", "logs", "-f", "--tail", str(tail), container],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log_procs[container] = (p, generation)
        log_tasks[container] = asyncio.create_task(log_reader(container, p, generation))

    def stop_log_stream(container: str, generation: int):
        current = log_procs.get(container)
        if not current or current[1] != generation:
            return
        p, _ = log_procs.pop(container)
        task = log_tasks.pop(container, None)
        if p:
            _terminate_proc(p)
        if task:
            task.cancel()

    async def recv_loop():
        try:
            while not stop.is_set():
                msg = await websocket.receive_json()
                t = msg.get("type")
                if t == "log_subscribe":
                    container = msg.get("container")
                    if container:
                        # 历史回放与实时追踪共用一个 docker logs 进程，不存在两次请求
                        # 之间的丢行窗口；限制回放量，避免恶意控制消息放大资源占用。
                        try:
                            tail = max(0, min(int(msg.get("tail", 1000)), 5000))
                        except (TypeError, ValueError):
                            tail = 1000
                        try:
                            generation = int(msg["generation"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if generation <= 0:
                            continue
                        start_log_stream(container, tail, generation)
                elif t == "log_unsubscribe":
                    try:
                        generation = int(msg["generation"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if generation <= 0:
                        continue
                    stop_log_stream(msg.get("container", ""), generation)
        except Exception:  # noqa: BLE001 - 断连/协议错误
            pass

    m_task = asyncio.create_task(metrics_loop())
    e_task = asyncio.create_task(docker_events_loop())
    recv_task = asyncio.create_task(recv_loop())
    try:
        while not stop.is_set():
            msg = await send_q.get()
            try:
                await websocket.send_json(msg)
            except Exception:  # noqa: BLE001 - 对端断开
                break
    except Exception:  # noqa: BLE001
        pass
    finally:
        stop.set()
        m_task.cancel()
        e_task.cancel()
        recv_task.cancel()
        for p, _ in list(log_procs.values()):
            _terminate_proc(p)
        log_procs.clear()
        log_tasks.clear()
        if on_progress in _progress_listeners:
            _progress_listeners.remove(on_progress)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("FW_AGENT_PORT", 9000)))
