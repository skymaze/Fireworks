#!/usr/bin/env bash
# DGX Agent 部署脚本（由控制平面通过 SSH 上传后执行）
# 用法: deploy.sh <agent_port> <workdir>
#
# 支持两种模式：
#   1) root 或免密 sudo  -> 安装为 systemd 系统服务（/etc/systemd/system）
#   2) 普通用户          -> 安装到用户目录，nohup 后台运行（无 root 依赖）
set -euo pipefail

AGENT_PORT="${1:-9000}"
WORKDIR="${2:-$HOME/.dgx-agent}"
VENV="$WORKDIR/venv"
# 控制平面下发的共享 token（由后端以 DGX_AGENT_TOKEN 环境变量传入）；
# 未提供时 Agent 会 fail closed 拒绝一切请求——即视为部署配置错误。
# token 用作命令/文件内容，收紧字符集防注入。
TOKEN="${DGX_AGENT_TOKEN:-}"
case "$TOKEN" in
  *[!A-Za-z0-9_-]*) echo "[deploy] 非法 DGX_AGENT_TOKEN 字符（仅允许字母数字 - _）" >&2; exit 1 ;;
esac

echo "[deploy] 安装目录: $WORKDIR, 端口: $AGENT_PORT, 用户: $(id -un)"

# 0. 确定是否需要 sudo（写 /etc/systemd/system）
NEED_SUDO=""
if [ "$(id -u)" != "0" ]; then
  if sudo -n true 2>/dev/null; then
    NEED_SUDO="sudo"
  else
    echo "[deploy] 无 root/免密 sudo，使用用户态部署（nohup 后台运行）"
    NEED_SUDO=""
  fi
fi

# 1. 依赖安装（venv 隔离）
if command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "[deploy] 未找到 python3" >&2
  exit 1
fi

mkdir -p "$WORKDIR"
if [ ! -d "$VENV" ]; then
  echo "[deploy] 创建 venv..."
  "$PY" -m venv "$VENV"
fi
echo "[deploy] 安装 Python 依赖..."
# 加超时/重试：PyPI 网络抖动时快速失败而非无限挂起（挂起的 deploy.sh 会阻塞后续部署）
"$VENV/bin/pip" install --quiet --timeout 30 --retries 3 --upgrade pip
"$VENV/bin/pip" install --quiet --timeout 30 --retries 3 -r "$WORKDIR/requirements.txt"

# 2. 启动方式
if [ -n "$NEED_SUDO" ] || [ "$(id -u)" = "0" ]; then
  # ---- systemd 系统服务 ----
  SUDO="${NEED_SUDO:-}"
  cat > "$WORKDIR/dgx-agent.service" <<EOF
[Unit]
Description=DGX Spark Agent (Fireworks)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$VENV/bin/uvicorn main:app --host 0.0.0.0 --port $AGENT_PORT
WorkingDirectory=$WORKDIR
Environment=DGX_AGENT_PORT=$AGENT_PORT
Environment=DGX_AGENT_WORKDIR=$WORKDIR/work
Environment=DGX_AGENT_TOKEN=$TOKEN
Restart=always
RestartSec=3
StandardOutput=append:$WORKDIR/agent.log
StandardError=append:$WORKDIR/agent.log

[Install]
WantedBy=multi-user.target
EOF
  chmod 600 "$WORKDIR/dgx-agent.service"
  $SUDO cp "$WORKDIR/dgx-agent.service" /etc/systemd/system/dgx-agent.service
  # unit 内含明文 token：收紧为仅 root 可读，防节点本地用户窥探
  $SUDO chmod 600 /etc/systemd/system/dgx-agent.service
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable dgx-agent.service
  $SUDO systemctl restart dgx-agent.service
else
  # ---- 用户态：nohup + setsid 后台运行（SSH 断开后仍存活，不依赖 systemd --user）----
  # 先停掉可能存在的旧实例（含 systemd --user 服务，避免抢端口）
  systemctl --user stop dgx-agent.service 2>/dev/null || true
  systemctl --user disable dgx-agent.service 2>/dev/null || true
  pkill -f "uvicorn main:app.*$AGENT_PORT" 2>/dev/null || true
  sleep 1
  cd "$WORKDIR"
  # token 经 0600 配置文件注入并导出：不落入命令行/argv（对 `ps` 与其它用户不可见），
  # 进程从环境变量读取 DGX_AGENT_TOKEN（与 systemd Environment= 行为一致）
  printf 'DGX_AGENT_TOKEN=%s\n' "$TOKEN" > "$WORKDIR/token.env"
  chmod 600 "$WORKDIR/token.env"
  set -a
  . "$WORKDIR/token.env"
  set +a
  setsid nohup env DGX_AGENT_PORT="$AGENT_PORT" DGX_AGENT_WORKDIR="$WORKDIR/work" \
    "$VENV/bin/uvicorn" main:app --host 0.0.0.0 --port "$AGENT_PORT" \
    >> "$WORKDIR/agent.log" 2>&1 < /dev/null &
  unset DGX_AGENT_TOKEN
  echo "[deploy] nohup 启动 PID $!"
fi

# 3. 等待服务就绪
echo "[deploy] 等待服务启动..."
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$AGENT_PORT/api/health" >/dev/null 2>&1; then
    echo "[deploy] 服务已就绪 (端口 $AGENT_PORT)"
    exit 0
  fi
  sleep 1
done
echo "[deploy] 服务启动超时，请检查 $WORKDIR/agent.log" >&2
exit 1
