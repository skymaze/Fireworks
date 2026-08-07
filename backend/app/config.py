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

# Agent
AGENT_DEPLOY_DIR = os.environ.get("AGENT_DEPLOY_DIR", "/opt/dgx-agent")

# HTTP 超时（控制平面 -> Agent）
AGENT_HTTP_TIMEOUT = 15
COMPOSE_UP_TIMEOUT = 600

# 任务健康检查（发布后轮询 vLLM /v1/models 的最大秒数）
# 900s 覆盖首次模型加载+初始化（3-5 分钟）与端口/竞态等待
TASK_HEALTH_TIMEOUT = _int("TASK_HEALTH_TIMEOUT", 900)
TASK_HEALTH_INTERVAL = 5

# 模型管理：控制平面本地模型缓存目录（HF 下载 -> 管理网发送 head -> RoCE 同步 worker）
MODEL_CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", "./models-cache")

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
