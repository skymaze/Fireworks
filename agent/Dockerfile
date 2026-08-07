# Fireworks 节点 Agent 镜像：构建时安装全部依赖，运行时零网络。
# 参考 Portainer Agent：节点经 SSH 执行 docker load + docker run，
# 挂载 docker.sock 与宿主工具链（nvidia-smi / ib_* / 动态库）。
#
# 平台：linux/amd64 + linux/arm64（CI buildx 双架构，docker CLI 按 TARGETARCH 下载）。
FROM python:3.13-slim

ARG TARGETARCH
# 构建期下载源（国内构建可覆盖为镜像站）：
#   DOCKER_STATIC_BASE=https://download.docker.com/linux/static/stable
#   COMPOSE_BASE=https://github.com/docker/compose/releases/download
ARG DOCKER_STATIC_BASE=https://download.docker.com/linux/static/stable
ARG COMPOSE_BASE=https://github.com/docker/compose/releases/download

# docker CLI + compose 插件：官方静态二进制（与宿主 docker daemon 版本无关，走 sock 通信）
# iperf3 / ssh / rsync / ping：网络测试与 head→worker 模型/镜像同步
RUN ARCH=$([ "$TARGETARCH" = "arm64" ] && echo aarch64 || echo x86_64) \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl iperf3 openssh-client rsync iputils-ping \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "${DOCKER_STATIC_BASE}/${ARCH}/docker-27.5.1.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "${COMPOSE_BASE}/v2.35.0/docker-compose-linux-${ARCH}" \
         -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# 构建上下文为仓库根（CI 与 backend/frontend 一致：context: . / file: agent/Dockerfile）
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY agent/main.py .

# HOME=/data：Path.home() 的路径（~/.fireworks-images、~/.cache/huggingface、
# ~/.ssh）全部落在挂载的数据目录，agent 代码零改动（部署时 -v <dir>:/data）。
# 注意：main.py 放 /app 而非 /data——/data 会被宿主数据目录挂载遮盖。
ENV HOME=/data

EXPOSE 9000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
