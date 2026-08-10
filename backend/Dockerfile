# Python 3.13（当前主流稳定版；3.11 安全支持 2027-10 到期）
FROM python:3.13-slim

WORKDIR /app

# 构建上下文为仓库根（见 docker-compose*.yml：context: . / dockerfile: backend/Dockerfile）

# 镜像分发由内置 registry 客户端完成，不依赖宿主 docker daemon 或外部 CLI。
# git：配方源（FireworksRecipes 浅克隆/拉取镜像目录）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
# agent 部署文件（main.py/requirements.txt/deploy.sh）——容器内「部署 Agent」
# 依赖这些文件，deploy_agent.py 从 /app/agent 读取（LOCAL_AGENT_DIR 回退解析）。
COPY agent ./agent
# wheelhouse：节点 Agent 离线依赖（构建时一次拉齐，节点部署零网络）。
# 按 Python 3.10-3.13 × linux/amd64+aarch64 矩阵生成（pydantic_core/websockets 等
# 无 abi3 wheel，绑定 Python 小版本与架构；pip download 多 --platform 只取其一，
# 故按平台分开下载合并）。deploy.sh 按节点 Python 版本选子目录。
RUN for v in 3.10 3.11 3.12 3.13; do \
      for arch in x86_64 aarch64; do \
        pip download --only-binary=:all: \
          --platform "manylinux2014_${arch}" \
          --python-version "$v" --implementation cp \
          -d "/app/agent/wheels/$v" -r /app/agent/requirements.txt; \
      done; \
    done && find /app/agent/wheels -name "*.whl" | wc -l

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
