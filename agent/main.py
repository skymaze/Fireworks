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
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

APP_VERSION = "0.1.0"


def resolve_workdir() -> Path:
    """工作目录：优先 DGX_AGENT_WORKDIR，其次 /opt/dgx-agent，最后用户目录/tmp。"""
    candidates = [
        os.environ.get("DGX_AGENT_WORKDIR"),
        "/opt/dgx-agent/work",
        str(Path.home() / ".dgx-agent" / "work"),
        "/tmp/dgx-agent/work",
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
    return Path("/tmp/dgx-agent/work")


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
# 鉴权：控制平面共享 token（由部署脚本写入 DGX_AGENT_TOKEN）
#
# 所有端点（除 /api/health 探针）与 /ws/events 均要求携带合法 token：
# - Authorization: Bearer <token> 或 X-Agent-Token 或 ?token=；
# - 恒时比较防时序侧信道；
# - 未配置 DGX_AGENT_TOKEN 时 fail closed：拒绝一切请求，
#   避免因忘记下发 token 导致 Agent 裸奔在网络上。
# ---------------------------------------------------------------------------

AGENT_TOKEN = os.environ.get("DGX_AGENT_TOKEN", "").strip()

if not AGENT_TOKEN:
    print("[agent] 警告: 未设置 DGX_AGENT_TOKEN，Agent 将拒绝所有请求（fail closed）", flush=True)


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
    """nvidia-smi 查询 GPU 基本信息。"""
    out, rc, _ = run_cmd(
        [
            "nvidia-smi",
            "--query-gpu=index,name,uuid,memory.total,memory.free,"
            "temperature.gpu,utilization.gpu,power.draw,power.limit,"
            "compute_cap,driver_version",
            "--format=csv,noheader,nounits",
        ],
        timeout=15,
    )
    if rc != 0:
        return []
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

    - \n 结尾   -> 完整行（update=False；若前一段是原地刷新链，则该段视为
                  刷新行最终态，update=True——docker 把整条进度条作为一个
                  带 \n 的条目交付，最后一段必须覆盖而不是拼接）
    - \r 结尾   -> 原地刷新行（进度类本行覆盖，update=True）
    - \r\n      -> 完整行（CRLF，update=False）

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
                update = prev_update
                out.append((seg, update))
                prev_update = update
        elif idx_n == idx_r + 1:  # \r\n
            seg, buf = buf[:idx_r], buf[idx_n + 1:]
            if seg:
                out.append((seg, False))
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
    d = _compose_dir(project)
    (d / "compose.yml").write_text(req.compose_yaml, encoding="utf-8")
    # env key 白名单 + 剥离 CR/LF：防换行注入 compose 插值产生额外环境变量
    env_lines = []
    for k, v in req.env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise HTTPException(400, f"非法环境变量名: {k!r}")
        env_lines.append(f"{k}={str(v).replace(chr(10), '').replace(chr(13), '')}")
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
                 f"/tmp/dgx-iperf3-{req.port}.log"],
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
            logf = open(f"/tmp/dgx-{req.tool}-{req.port}.log", "w")
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
# 模型管理（接收 / 同步 / 列表 / 删除）
#   - 下载由控制平面完成（管理网），经 /api/model/receive 流式上传到 head；
#   - head 再经 /api/model/sync 通过 RoCE 高速计算网同步到 worker。
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
    url: str          # 控制平面文件路径（相对路径；完整 URL 兼容）
    size: int = 0
    symlink: str | None = None


def _resolve_pull_url(client_ip: str, url: str) -> str:
    """回拉地址解析：后端下发的相对路径 → 用「下发请求的来源 IP」补全控制端地址。

    控制平面 -> Agent 的请求来自控制平面管理网出口 IP（docker 部署经宿主机 NAT，
    节点看到的正是宿主机管理网 IP），Agent 据此回拉恰好可达——控制端换机/换 IP
    后无需任何配置。控制平面 API 端口为部署约定（8000）。

    安全收缩（防 SSRF）：
    - 绝对 URL 仅允许指向「控制平面来源 IP:8000」或本机回环（127.0.0.1/localhost，
      测试与本地回环用；能下发拉取命令者已具备控制平面凭据=节点 RCE，回环不扩大面）；
    - 其余一律 400，禁止被诱导拉取内网/云元数据等任意内网地址；
    - 重定向不跟随（见 _open_control_plane）。
    """
    if url.startswith(("http://", "https://")):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in ("127.0.0.1", "::1", "localhost"):
            port = parts.port or (80 if parts.scheme == "http" else 443)
            if not client_ip or host != client_ip or port != 8000:
                raise HTTPException(400, "非法回拉 URL（仅允许控制平面自身）")
        return url
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


@app.post("/api/model/pull")
def model_pull(req: ModelPullRequest, request: Request):
    """从控制平面（管理网）拉取模型文件（GET 流式下载，断点续传）。

    中断保留 .part 分片（不删除），重试带 Range: bytes=N- 从断点续传；
    完成后按总大小校验并 rename。控制平面 FileResponse 支持 Range（206）。
    """
    pull_url = _resolve_pull_url(
        request.client.host if request.client else "", req.url
    )
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
    if target.exists() and target.stat().st_size == req.size:
        return {"ok": True, "relpath": req.relpath, "skipped": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0
    if have >= req.size:
        # 残留分片已完整/超长：删除重下
        tmp.unlink(missing_ok=True)
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        with _open_control_plane(pull_url, headers, 600) as resp, \
                open(tmp, "ab" if have else "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                have += len(chunk)
                notify_progress("model", f"{req.repo}/{req.relpath}", have, req.size)
        total = tmp.stat().st_size
        if req.size and total != req.size:
            raise HTTPException(400, f"大小不匹配: {total} != {req.size}")
        tmp.rename(target)
    except Exception:
        # 中断保留 .part，下次请求自动 Range 续传
        raise
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


class ModelSyncRequest(BaseModel):
    target_host: str
    target_user: str = "spark"
    target_port: int = 22
    repo: str
    cache_dir: str | None = None


def _do_sync(job_id: str, req: ModelSyncRequest) -> None:
    job = _model_jobs[job_id]
    try:
        src = _model_dir(req.repo, req.cache_dir)
        if not src.exists():
            raise RuntimeError(f"源缓存不存在: {src}")
        # 目标目录：HF hub 布局
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "--", req.repo)
        dst = f"{req.target_user}@{req.target_host}:~/.cache/huggingface/hub/models--{safe}/"
        # 先确保目标 hub 目录存在（ssh 建目录）
        ssh_base = [
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-p", str(req.target_port),
            f"{req.target_user}@{req.target_host}",
        ]
        r = subprocess.run(ssh_base + ["mkdir -p ~/.cache/huggingface/hub"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"目标目录准备失败: {r.stderr.strip()}")
        # rsync 走 SSH（accept-new 自动记录 host key），只同步权重与配置，
        # 排除 in-flight 临时文件（*.incomplete / *.lock / *.part 分片）。
        # 注意：-e 只传 ssh 选项，主机由 rsync 根据 dst 的 user@host 自行追加
        remote_shell = " ".join([
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-p", str(req.target_port),
        ])
        rsync = [
            # RoCE 高速网下去 -z（权重/层数据已近随机，压缩纯耗 CPU 且拖慢传输）；
            # --partial --inplace：中断后从断点续传，不整文件重传
            "rsync", "-a", "--delete", "--partial", "--inplace",
            "-e", remote_shell,
            "--exclude=*.incomplete", "--exclude=*.lock", "--exclude=*.part",
            "--exclude=.huggingface",
            f"{src}/", dst,
        ]
        proc = subprocess.Popen(rsync, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate(timeout=7200)
        if proc.returncode != 0:
            raise RuntimeError(f"rsync 失败: {err.strip()[:500]}")
        job.update(status="completed")
    except Exception as e:  # noqa: BLE001
        job.update(status="failed", error=str(e))


@app.post("/api/model/sync")
def model_sync(req: ModelSyncRequest):
    """通过 SSH/rsync 把本机模型同步到目标节点（走 RoCE 高速计算网地址）。"""
    job_id = uuid.uuid4().hex[:12]
    _model_jobs[job_id] = {"kind": "sync", "status": "running", "repo": req.repo,
                           "target": req.target_host, "started": time.time(), "error": None}
    t = threading.Thread(target=_do_sync, args=(job_id, req), daemon=True)
    t.start()
    return {"job_id": job_id}


@app.get("/api/model/sync/{job_id}")
def model_sync_status(job_id: str):
    job = _model_jobs.get(job_id)
    _prune_jobs(_model_jobs)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


# ---------- 镜像分发（管理平面 skopeo 拉取 -> 本节点 docker load / RoCE 同步） ----------

IMAGE_DIR = Path.home() / ".dgx-images"

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
    url: str          # 控制平面归档路径（相对路径；完整 URL 兼容）


@app.post("/api/image/pull")
def image_pull(req: ImagePullRequest, request: Request):
    """从控制平面拉取镜像归档（GET 流式，断点续传：中断保留 .part，重试带 Range 续传）。"""
    if not req.digest:
        raise HTTPException(400, "缺少 digest")
    pull_url = _resolve_pull_url(
        request.client.host if request.client else "", req.url
    )
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    target = IMAGE_DIR / f"{req.digest}.tar"
    if target.exists() and target.stat().st_size > 0:
        return {"ok": True, "skipped": True, "path": str(target)}
    tmp = target.with_name(target.name + ".part")
    have = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        with _open_control_plane(pull_url, headers, 3600) as resp, \
                open(tmp, "ab" if have else "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                have += len(chunk)
                notify_progress("image", req.digest, have, 0)  # 归档总大小未知
        tmp.rename(target)
    except Exception:
        raise  # 中断保留 .part，下次自动续传
    return {"ok": True, "bytes": target.stat().st_size, "path": str(target)}


class ImageLoadRequest(BaseModel):
    image: str
    digest: str = ""


@app.post("/api/image/load")
def image_load(req: ImageLoadRequest):
    """docker load 镜像归档并校验 digest（归档文件 sha256 指纹）。

    digest 为归档文件指纹（构建确定性，跨节点字节一致）。跳过条件：
    本节点已用该归档指纹 load 成功过（标记文件 .loaded-<digest>，
    比 docker RepoDigests 可靠——load 镜像无 registry digest，且避免
    旧版本同名镜像误判已加载）。load 成功后写入标记。
    """
    if not req.digest:
        raise HTTPException(400, "缺少 digest")
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
    # 该指纹已 load 成功过 -> 跳过（幂等）
    mark = IMAGE_DIR / f".loaded-{req.digest}"
    if mark.exists():
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


class ImageSyncRequest(BaseModel):
    target_host: str
    target_user: str = "spark"
    target_port: int = 22
    image: str
    digest: str = ""


def _image_do_sync(job_id: str, req: ImageSyncRequest) -> None:
    job = _image_jobs[job_id]
    try:
        src = IMAGE_DIR / f"{req.digest}.tar"
        if not src.exists():
            raise RuntimeError(f"归档不存在: {src}")
        dst = f"{req.target_user}@{req.target_host}:~/.dgx-images/"
        ssh_base = [
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-p", str(req.target_port),
            f"{req.target_user}@{req.target_host}",
        ]
        r = subprocess.run(ssh_base + ["mkdir -p ~/.dgx-images"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"目标目录准备失败: {r.stderr.strip()[:200]}")
        remote_shell = " ".join([
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", "-p", str(req.target_port),
        ])
        # rsync 重试 3 次（瞬时 SSH/网络抖动可恢复；镜像文件大，超时放宽）。
        # 去 -z + --partial --inplace：RoCE 下压缩是瓶颈，中断后断点续传
        last_err = ""
        for attempt in range(3):
            proc = subprocess.Popen(
                ["rsync", "-a", "--partial", "--inplace", "-e", remote_shell,
                 f"{src}", dst],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            _, err = proc.communicate(timeout=7200)
            if proc.returncode == 0:
                job.update(status="completed")
                return
            last_err = err.strip()[:300]
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"rsync 失败（重试3次）: {last_err}")
    except Exception as e:  # noqa: BLE001
        job.update(status="failed", error=str(e))


_image_jobs: dict[str, dict] = {}


def _prune_jobs(jobs: dict, ttl: float = 3600) -> None:
    """清理已完成/失败超过 TTL 的同步任务记录（防内存无界增长）。"""
    now = time.time()
    stale = [
        k for k, j in jobs.items()
        if j.get("status") in ("completed", "failed") and now - j.get("started", 0) > ttl
    ]
    for k in stale:
        jobs.pop(k, None)


@app.post("/api/image/sync")
def image_sync(req: ImageSyncRequest):
    """把本机镜像归档 rsync 到目标节点（走 RoCE 高速计算网）。"""
    job_id = uuid.uuid4().hex[:12]
    _image_jobs[job_id] = {"kind": "image-sync", "status": "running", "image": req.image,
                           "target": req.target_host, "started": time.time(), "error": None}
    threading.Thread(target=_image_do_sync, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/image/sync/{job_id}")
def image_sync_status(job_id: str):
    job = _image_jobs.get(job_id)
    _prune_jobs(_image_jobs)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


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

WS_METRICS_INTERVAL = float(os.environ.get("DGX_WS_METRICS_INTERVAL", "5"))


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    """后端实时数据通道。

    推送：metrics（每 5s）、docker_event（容器生命周期）、log（docker logs -f 行）、
    progress（模型/镜像拉取进度）、log_end（日志流结束）。
    接收：log_subscribe {container, tail} / log_unsubscribe {container}。
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
    log_procs: dict[str, subprocess.Popen] = {}
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

    async def log_reader(container: str, proc: subprocess.Popen):
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
                          "update": update})
                if len(buf) > (1 << 20):  # 无分隔符巨型段防堆积：按完整行追加
                    push({"type": "log", "container": container,
                          "line": buf.decode("utf-8", errors="replace")})
                    buf = b""
                    prev_update = False
        finally:
            if buf:
                push({"type": "log", "container": container,
                      "line": buf.decode("utf-8", errors="replace")})
            push({"type": "log_end", "container": container})
            log_procs.pop(container, None)
            log_tasks.pop(container, None)

    def start_log_stream(container: str, tail: int):
        if container in log_procs:
            return
        p = subprocess.Popen(
            ["docker", "logs", "-f", "--tail", str(tail), container],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log_procs[container] = p
        log_tasks[container] = asyncio.create_task(log_reader(container, p))

    def stop_log_stream(container: str):
        p = log_procs.pop(container, None)
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
                        # tail 默认 0：流只推送订阅后的新行，历史快照由 HTTP 接口拉取
                        # （避免 --tail 回放与前端快照重叠产生重复行）
                        start_log_stream(container, int(msg.get("tail", 0)))
                elif t == "log_unsubscribe":
                    stop_log_stream(msg.get("container", ""))
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
        for p in list(log_procs.values()):
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

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DGX_AGENT_PORT", 9000)))
