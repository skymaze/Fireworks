"""控制平面 -> Agent 的 HTTP 客户端。

超时约定：
- 连接超时固定 5s（不可达节点快速失败，不被长任务总超时放大）；
- 读写超时按调用传参（长操作单独传 timeout），缺省 15s；
- 幂等读操作（GET/状态类）对连接类瞬时错误重试 2 次（1s/3s 退避）；
  写操作（compose/pull/load/delete 等）不重试，避免重复执行。
"""

import asyncio
import logging

import httpx
from fastapi import HTTPException

from .. import config
from ..errors import Code, api_error
from ..models import Node

logger = logging.getLogger(__name__)

# trust_env=False：管理网直连 IP 不应被 HTTP(S)_PROXY 环境变量劫持
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(config.AGENT_HTTP_TIMEOUT, connect=5.0),
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    trust_env=False,
)

# 连接类瞬时错误（可重试）；HTTP 状态错误（agent 明确返回 4xx/5xx）不重试
_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
              httpx.RemoteProtocolError, httpx.ReadError, httpx.NetworkError,
              httpx.StreamError, httpx.PoolTimeout)


async def close() -> None:
    """关闭连接池（lifespan 停机时调用）。"""
    await _client.aclose()


def base_url(node: Node) -> str:
    return f"http://{node.ip}:{node.agent_port}"


def _timeout(total: float | None) -> httpx.Timeout:
    """组装超时：总超时按调用传参，连接超时固定 5s（不可达节点快速失败）。"""
    if total is None:
        return httpx.Timeout(config.AGENT_HTTP_TIMEOUT, connect=5.0)
    return httpx.Timeout(total, connect=5.0)


async def _request(method: str, node: Node, path: str, *,
                   retry: bool = False, **kwargs):
    timeout = _timeout(kwargs.pop("timeout", None))
    url = base_url(node) + path
    # 控制平面 -> Agent 携带该节点自己的 token（agent 鉴权；NULL=未部署，
    # 空 Bearer 会被 agent fail closed 拒绝，属防御正确的行为）
    headers = dict(kwargs.pop("headers", None) or {})
    headers["Authorization"] = f"Bearer {node.agent_token or ''}"
    kwargs["headers"] = headers
    attempts = 3 if retry else 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await _client.request(method, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except _RETRYABLE as e:
            last_exc = e
            if attempt < attempts - 1:
                await asyncio.sleep(1 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def map_agent_error(e: Exception) -> HTTPException:
    """把 agent 调用异常映射为统一 HTTPException：
    连接/超时 -> 502 节点不可达；agent 404 -> 404；其余状态 -> 502 带 agent 错误信息。
    结构化：稳定 code + 中文 msg + 原始 body/异常原样放 details（不本地化）。
    """
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        body = (e.response.text or "")[:200]
        if status == 404:
            return api_error(404, Code.AGENT_RESOURCE_NOT_FOUND,
                             f"节点资源不存在: {body}", details=body)
        return api_error(502, Code.AGENT_EXEC_FAILED,
                         f"节点执行失败({status}): {body}", details=body)
    return api_error(502, Code.AGENT_UNREACHABLE, f"节点不可达: {e}",
                     details=str(e))


async def health(node: Node) -> bool:
    try:
        data = await _request("GET", node, "/api/health", retry=True)
        return data.get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


async def info(node: Node) -> dict:
    return await _request("GET", node, "/api/info", retry=True)


async def metrics(node: Node) -> dict:
    return await _request("GET", node, "/api/metrics", retry=True)


async def nvidia_smi(node: Node) -> str:
    data = await _request("GET", node, "/api/nvidia-smi", retry=True, timeout=25)
    return data.get("output", "")


async def http_get(node: Node, url: str, timeout: int = 10) -> dict:
    return await _request(
        "GET", node, "/api/http-get", params={"url": url, "timeout": timeout},
        retry=True,
    )


async def llm_probe(node: Node, payload: dict) -> dict:
    """推理服务探针：agent 向容器内 OpenAI 兼容端点发起流式请求并测吞吐/延迟。"""
    return await _request(
        "POST", node, "/api/probe/llm", json=payload,
        timeout=payload.get("timeout", 10) + 3,
    )


async def llm_benchmark(node: Node, payload: dict) -> dict:
    """推理服务并发 decode 压测：agent 侧多线程并发流式请求并聚合 tok/s。"""
    return await _request(
        "POST", node, "/api/probe/benchmark", json=payload,
        timeout=payload.get("timeout", 120) + 30,
    )


async def compose_up(node: Node, project: str, compose_yaml: str, env: dict) -> dict:
    return await _request(
        "POST",
        node,
        "/api/compose/up",
        json={"project": project, "compose_yaml": compose_yaml, "env": env},
        timeout=config.COMPOSE_UP_TIMEOUT,
    )


async def compose_down(node: Node, project: str) -> dict:
    return await _request(
        "POST", node, "/api/compose/down", json={"project": project}, timeout=120
    )


async def compose_ps(node: Node, project: str) -> dict:
    return await _request(
        "POST", node, "/api/compose/ps", json={"project": project}, timeout=10
    )


async def list_containers(node: Node) -> list:
    data = await _request("GET", node, "/api/containers", retry=True)
    return data.get("containers", [])


async def container_action(node: Node, name: str, action: str) -> dict:
    # agent 侧 docker pause/unpause/stop 超时 60s，后端给足余量避免误报
    return await _request(
        "POST", node, f"/api/containers/{name}/action", json={"action": action},
        timeout=65,
    )


async def container_logs(node: Node, name: str, tail: int = 200) -> str:
    data = await _request(
        "GET", node, f"/api/containers/{name}/logs", params={"tail": tail},
        retry=True,
    )
    return data.get("logs", "")


async def network_test(node: Node, payload: dict, duration: int = 10) -> dict:
    """网络测试：agent 侧 iperf3 最长 duration+30s / perftest duration+60s，超时给足余量。"""
    return await _request(
        "POST", node, "/api/network/test", json=payload,
        timeout=duration + 90,
    )


async def network_server_stop(node: Node, payload: dict) -> dict:
    return await _request("POST", node, "/api/network/server-stop", json=payload)


# ---------- 模型管理（接收/同步/列表/删除） ----------


async def model_cache(node: Node) -> dict:
    return await _request("GET", node, "/api/model/list", retry=True)


async def model_cache_repo(node: Node, repo: str) -> dict:
    # agent 侧逐文件校验大模型缓存较慢，超时放宽到 30s
    return await _request("GET", node, f"/api/model/cache/{repo}",
                          retry=True, timeout=30)


async def model_pull(node: Node, repo: str, relpath: str, url: str, size: int,
                     transfer_id: int,
                     symlink: str | None = None, hash_algo: str | None = None,
                     digest: str | None = None) -> dict:
    """让节点从控制平面文件服务拉取模型文件（管理网，GET 流式）。"""
    return await _request(
        "POST",
        node,
        "/api/model/pull",
        json={"repo": repo, "relpath": relpath, "url": url, "size": size,
              "symlink": symlink, "hash_algo": hash_algo, "digest": digest,
              "transfer_id": transfer_id},
        timeout=3600,
    )


async def model_delete(node: Node, repo: str) -> dict:
    # agent 侧 rmtree 大模型无超时，后端给 5 分钟上限
    return await _request("DELETE", node, f"/api/model/{repo}", timeout=300)


async def model_share(node: Node, repo: str) -> dict:
    """在 head Agent 为模型缓存签发短期共享令牌并生成 manifest。"""
    return await _request(
        "POST", node, "/api/model/share", json={"repo": repo, "ttl": 21600},
        timeout=3600,
    )


async def model_fetch(node: Node, payload: dict) -> dict:
    """启动 worker 从 head 高速网后台直拉模型。"""
    return await _request("POST", node, "/api/model/fetch", json=payload, timeout=120)


async def model_fetch_status(node: Node, job_id: str) -> dict:
    return await _request("GET", node, f"/api/model/fetch/{job_id}", retry=True)


async def model_fetch_cancel(node: Node, job_id: str) -> dict:
    return await _request("POST", node, f"/api/model/fetch/{job_id}/cancel", timeout=30)


# ---------- 镜像分发（拉取/加载/同步） ----------


async def image_pull(node: Node, image: str, digest: str, url: str, size: int) -> dict:
    """让节点从控制平面拉取镜像归档（管理网，GET 流式，断点续传）。"""
    return await _request(
        "POST", node, "/api/image/pull",
        json={"image": image, "digest": digest, "url": url, "size": size},
        timeout=7200,
    )


async def image_load(node: Node, image: str, digest: str) -> tuple[bool, str]:
    """节点 docker load + 归档指纹校验（已有该指纹标记 .loaded-<digest> 时跳过）。"""
    try:
        resp = await _request(
            "POST", node, "/api/image/load",
            json={"image": image, "digest": digest},
            timeout=3700,  # agent 侧 docker load 超时 3600s，后端给足余量
        )
        return bool(resp.get("ok")), resp.get("error") or ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def image_share(node: Node, digest: str) -> dict:
    """在 head Agent 为归档签发短期共享令牌。"""
    return await _request(
        "POST", node, "/api/image/share", json={"digest": digest, "ttl": 7200},
        timeout=120,
    )


async def image_fetch(node: Node, payload: dict) -> dict:
    """让 worker Agent 从 head 的高速网地址直接回拉归档。"""
    return await _request(
        "POST", node, "/api/image/fetch", json=payload, timeout=7200,
    )


async def image_status(node: Node, image: str) -> dict:
    """节点上指定镜像状态（是否存在该 tag，present=就绪判定用）。"""
    return await _request("GET", node, "/api/image/status",
                          params={"image": image}, retry=True)
