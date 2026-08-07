#!/usr/bin/env bash
# Fireworks Agent 容器部署脚本（由控制平面经 SSH 执行；参考 Portainer Agent）。
# 前置：agent 镜像 tar 已由控制平面传输到 $WORKDIR/agent-image.tar。
#
# 用法: FW_AGENT_TOKEN='<token>' bash deploy-container.sh <port> <dir> <arch> [image]
#   <port>  Agent 监听端口（默认 9000）
#   <dir>   数据目录（挂载为容器 /data，默认 /opt/fireworks-agent）
#   <arch>  节点架构 arm64|amd64（决定挂载的宿主 lib 目录）
#   <image> Agent 镜像引用（默认 ghcr.io/skymaze/fireworks-agent:latest；中国大陆部署
#           传阿里云地址，如 registry.cn-shanghai.aliyuncs.com/aixn-public/fireworks-agent:latest）
set -euo pipefail

AGENT_PORT="${1:-9000}"
WORKDIR="${2:-/opt/fireworks-agent}"
ARCH="${3:-arm64}"
IMAGE="${4:-ghcr.io/skymaze/fireworks-agent:latest}"
CONTAINER="fireworks-agent"
IMAGE_ARCHIVE="$WORKDIR/agent-image.tar"

TOKEN="${FW_AGENT_TOKEN:-}"
case "$TOKEN" in
  *[!A-Za-z0-9_-]*) echo "[deploy] 非法 FW_AGENT_TOKEN 字符（仅允许字母数字 - _）" >&2; exit 1 ;;
esac

# 容器化部署的硬前提：节点 docker 可用（root 或 docker 组权限）
if ! docker version >/dev/null 2>&1; then
  echo "[deploy] 节点 docker 不可用：请确认 docker 已安装且当前用户有权限（root 或 docker 组）" >&2
  exit 1
fi

# 数据目录 + SSH 互信密钥对（head→worker 模型/镜像 rsync 免密；容器内 $HOME/.ssh）
mkdir -p "$WORKDIR/work" "$WORKDIR/.ssh"
chmod 700 "$WORKDIR/.ssh"
[ -f "$WORKDIR/.ssh/id_ed25519" ] || ssh-keygen -t ed25519 -N '' -f "$WORKDIR/.ssh/id_ed25519" -q
# 宿主挂载快照（磁盘指标取宿主视角）：不能直接挂载 /proc/mounts（runc proc-safety
# 拒绝 /proc 内路径作挂载源），改为部署时快照到数据目录，容器内经 FW_HOST_MOUNTS 读取
cp /proc/mounts "$WORKDIR/host-mounts"

# 宿主 lib 目录 + 动态链接器（按架构）
case "$ARCH" in
  amd64|x86_64) HOST_LIB="/usr/lib/x86_64-linux-gnu"; HOST_LOADER="/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" ;;
  arm64|aarch64) HOST_LIB="/usr/lib/aarch64-linux-gnu"; HOST_LOADER="/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1" ;;
  *) echo "[deploy] 未知架构 $ARCH" >&2; exit 1 ;;
esac

# 宿主工具 wrapper（nvidia-smi / ib_* 等驱动匹配二进制）：显式用容器内 loader +
# 宿主库目录，只影响这些工具——不能全局设 LD_LIBRARY_PATH（会污染容器自身
# 二进制导致 glibc 冲突）。容器内 PATH=/host-bin 命中 wrapper。
HOST_TOOLS="nvidia-smi ib_write_bw ib_read_bw ibstat ibv_devinfo ibping"
mkdir -p "$WORKDIR/.host-bin"
for tool in $HOST_TOOLS; do
  if [ -x "/usr/bin/$tool" ]; then
    # 工具本体来自挂载的宿主 /usr/bin（/host-usr-bin），宿主库经 --library-path 提供
    printf '#!/bin/sh\nexec "%s" --library-path "%s" /host-usr-bin/%s "$@"\n' \
      "$HOST_LOADER" "$HOST_LIB" "$tool" > "$WORKDIR/.host-bin/$tool"
    chmod +x "$WORKDIR/.host-bin/$tool"
  fi
done

echo "[deploy] 加载镜像..."
docker load -i "$IMAGE_ARCHIVE" >/dev/null

# 幂等重部署：删旧容器（旧容器停掉即切换）
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo "[deploy] 启动容器 (端口 $AGENT_PORT)..."
docker run -d --name "$CONTAINER" \
  --restart unless-stopped \
  --network host \
  -e HOME=/data \
  -e FW_AGENT_TOKEN="$TOKEN" \
  -e FW_AGENT_PORT="$AGENT_PORT" \
  -e FW_AGENT_WORKDIR=/data/work \
  -e PATH="/usr/local/bin:/usr/bin:/host-bin:/bin" \
  -v "$WORKDIR:/data" \
  -v "$WORKDIR/.host-bin:/host-bin:ro" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /usr/bin:/host-usr-bin:ro \
  -v "$HOST_LIB:/host-usr-lib:ro" \
  -v /proc/driver/nvidia:/proc/driver/nvidia:ro \
  -e FW_HOST_MOUNTS=/data/host-mounts \
  --health-cmd "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:$AGENT_PORT/api/health', timeout=3).status == 200 else 1)\"" \
  --health-interval 15s --health-start-period 10s --health-retries 5 \
  "$IMAGE" >/dev/null

# 等待健康（不依赖节点 curl/python，直接用 docker inspect 健康状态）
echo "[deploy] 等待服务启动..."
for i in $(seq 1 60); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo starting)
  if [ "$status" = "healthy" ]; then
    echo "[deploy] 服务已就绪 (端口 $AGENT_PORT)"
    exit 0
  fi
  if [ "$status" = "unhealthy" ]; then
    echo "[deploy] 服务启动失败，容器日志:" >&2
    docker logs --tail 30 "$CONTAINER" >&2 || true
    exit 1
  fi
  sleep 1
done
echo "[deploy] 服务启动超时：docker logs $CONTAINER" >&2
exit 1
