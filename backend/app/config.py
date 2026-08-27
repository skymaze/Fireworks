"""控制平面配置（环境变量驱动）。"""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# 发布版本（控制平面/Agent/前端随仓库同源，升级提醒以此为"期望版本"基准）
APP_VERSION = "0.5.2"

# 数据库
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./fireworks.db")

# 指标采集
METRIC_POLL_INTERVAL = _int("METRIC_POLL_INTERVAL", 5)       # 秒
METRIC_RETENTION_HOURS = _int("METRIC_RETENTION_HOURS", 24)  # 小时
INFERENCE_RETENTION_HOURS = _int("INFERENCE_RETENTION_HOURS", 25)  # 24h + 差分基线

# Agent WS 心跳看门狗：WS 连接存活但超过该秒数无任何消息（agent 每
# METRIC_POLL_INTERVAL 必推 metrics 即应用级心跳）=> 判定节点离线并强制重连。
NODE_STALE_TIMEOUT = _int("NODE_STALE_TIMEOUT", 20)

# Agent
AGENT_DEPLOY_DIR = os.environ.get("AGENT_DEPLOY_DIR", "/opt/fireworks-agent")

# HTTP 超时（控制平面 -> Agent）
AGENT_HTTP_TIMEOUT = 15
COMPOSE_UP_TIMEOUT = 600

# 任务健康检查（发布后轮询 vLLM /v1/models 的最大秒数）
# 900s 覆盖首次模型加载+初始化（3-5 分钟）与端口/竞态等待
TASK_HEALTH_TIMEOUT = _int("TASK_HEALTH_TIMEOUT", 900)
TASK_HEALTH_INTERVAL = 5

# 推理服务统计（实时服务监控）：对 running 且含 VLLM_PORT 的任务周期性读取
# vLLM Prometheus /metrics 做区间差分，统计真实推理流量（tok/s / 请求 / TTFT /
# KV cache）。被动读取，不发送合成推理请求；空闲边界快照保证吞吐时间分母准确。
LLM_STATS_ENABLED = os.environ.get("LLM_STATS_ENABLED", "true").lower() in ("1", "true", "yes")
LLM_STATS_INTERVAL = _int("LLM_STATS_INTERVAL", 5)          # 秒

# 模型管理：控制平面缓存（HF 下载 -> 管理网发送 head -> Agent 高速直传 worker）
MODEL_CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", "./models-cache")

# 配方源（FireworksRecipes）：git 仓库镜像目录（同步只刷这里，不写 recipes 表）+ 默认源
RECIPE_SRC_DIR = os.environ.get("RECIPE_SRC_DIR", "./recipes-src")
RECIPE_DEFAULT_URL = os.environ.get(
    "RECIPE_DEFAULT_URL", "https://github.com/skymaze/FireworksRecipes.git"
)
RECIPE_DEFAULT_BRANCH = os.environ.get("RECIPE_DEFAULT_BRANCH", "main")
# 配方源 git 同步 timeout（秒；浅克隆小仓库足够）
RECIPE_SYNC_TIMEOUT = _int("RECIPE_SYNC_TIMEOUT", 180)

# ---------- 认证与安全 ----------

# 登录会话有效期（小时）；到期后需重新登录
SESSION_TTL_HOURS = _int("SESSION_TTL_HOURS", 168)

# 允许跨域来源（逗号分隔）。同源部署（经前端 /api 代理）下 CORS 基本不参与，
# 主要覆盖「前端开发服务器 -> 后端」等场景；生产建议保持最小集合。
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

# 会话 cookie 名
SESSION_COOKIE = "fw_session"

# 会话 cookie 是否标记 Secure（HTTPS 部署时开启，避免明文 HTTP 传输）
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
