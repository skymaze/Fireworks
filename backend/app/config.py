"""控制平面配置（环境变量驱动）。"""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# 数据库
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./fireworks.db")

# 指标采集
METRIC_POLL_INTERVAL = _int("METRIC_POLL_INTERVAL", 5)       # 秒
METRIC_RETENTION_HOURS = _int("METRIC_RETENTION_HOURS", 24)  # 小时

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

# LLM 探针（实时推理服务监控）：对 running 且含 VLLM_PORT 的任务周期性探测
# 实时 tok/s / TTFT / ITL / KV cache（auto_sampling 关停可避免干扰关键演示）
LLM_PROBE_ENABLED = os.environ.get("LLM_PROBE_ENABLED", "true").lower() in ("1", "true", "yes")
LLM_PROBE_INTERVAL = _int("LLM_PROBE_INTERVAL", 5)          # 秒
LLM_PROBE_MAX_TOKENS = _int("LLM_PROBE_MAX_TOKENS", 16)     # 每轮探针生成的最大 token（小值省负载）

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
