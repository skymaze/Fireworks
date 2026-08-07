FROM python:3.11-slim

WORKDIR /app

# 构建上下文为仓库根（见 docker-compose*.yml：context: . / dockerfile: backend/Dockerfile）

# skopeo：镜像分发（拉取/导出 docker-archive），直接做 HTTP 请求，
# 支持代理环境变量（settings.docker_proxy 注入）；不依赖宿主 docker daemon。
RUN apt-get update \
    && apt-get install -y --no-install-recommends skopeo ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
# agent 安装包（main.py/requirements.txt/deploy.sh）——容器内「部署 Agent」
# 依赖这些文件，deploy_agent.py 从 /app/agent 读取（LOCAL_AGENT_DIR 回退解析）。
COPY agent ./agent

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
