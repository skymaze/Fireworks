#!/usr/bin/env bash
# Fireworks Agent 部署脚本（由控制平面通过 SSH 上传后执行）
# 用法: deploy.sh <agent_port> <workdir>
#
# 支持两种模式：
#   1) root 或免密 sudo  -> 安装为 systemd 系统服务（/etc/systemd/system）
#   2) 普通用户          -> 安装为 systemd --user 服务并 enable-linger 开机自启
#                          （无 root 依赖；需可用 systemd --user，否则部署失败）
set -euo pipefail

AGENT_PORT="${1:-9000}"
WORKDIR="${2:-$HOME/.fireworks-agent}"
VENV="$WORKDIR/venv"
# 控制平面下发的该节点独立 token（由后端以 FW_AGENT_TOKEN 环境变量传入）；
# 未提供时 Agent 会 fail closed 拒绝一切请求——即视为部署配置错误。
# token 用作命令/文件内容，收紧字符集防注入。
TOKEN="${FW_AGENT_TOKEN:-}"
case "$TOKEN" in
  *[!A-Za-z0-9_-]*) echo "[deploy] 非法 FW_AGENT_TOKEN 字符（仅允许字母数字 - _）" >&2; exit 1 ;;
esac

echo "[deploy] 安装目录: $WORKDIR, 端口: $AGENT_PORT, 用户: $(id -un)"

# 0. 确定是否需要 sudo（写 /etc/systemd/system）
NEED_SUDO=""
if [ "$(id -u)" != "0" ]; then
  if sudo -n true 2>/dev/null; then
    NEED_SUDO="sudo"
  else
    echo "[deploy] 无 root/免密 sudo，使用用户态部署（systemd --user + enable-linger）"
    NEED_SUDO=""
  fi
fi

# 1. 依赖安装（venv 隔离，离线 wheelhouse）
if command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "[deploy] 未找到 python3" >&2
  exit 1
fi
# fastapi 0.141+ 要求 Python >= 3.10
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "[deploy] 需要 Python >= 3.10（当前: $($PY --version 2>&1)）" >&2; exit 1; }

mkdir -p "$WORKDIR"
if [ ! -d "$VENV" ]; then
  echo "[deploy] 创建 venv..."
  "$PY" -m venv "$VENV"
fi
# 离线安装：依赖由控制平面构建 backend 镜像时预下载（wheels/<py版本>/，随部署上传）。
# --no-index 完全断网安装，不受节点 PyPI 可达性/网络抖动影响。
echo "[deploy] 离线安装 Python 依赖..."
PYVER=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
WHEEL_DIR="$WORKDIR/wheels/$PYVER"
if [ ! -d "$WHEEL_DIR" ] || [ -z "$(ls -A "$WHEEL_DIR" 2>/dev/null)" ]; then
  echo "[deploy] 缺少 Python $PYVER 的离线依赖包（支持 3.10-3.13，或控制平面未上传 wheels）" >&2
  exit 1
fi
"$VENV/bin/pip" install --quiet --no-index --find-links="$WHEEL_DIR" -r "$WORKDIR/requirements.txt"

# 2. 启动方式
if [ -n "$NEED_SUDO" ] || [ "$(id -u)" = "0" ]; then
  # ---- systemd 系统服务 ----
  SUDO="${NEED_SUDO:-}"
  cat > "$WORKDIR/fireworks-agent.service" <<EOF
[Unit]
Description=Fireworks Agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$VENV/bin/uvicorn main:app --host 0.0.0.0 --port $AGENT_PORT
WorkingDirectory=$WORKDIR
Environment=FW_AGENT_PORT=$AGENT_PORT
Environment=FW_AGENT_WORKDIR=$WORKDIR/work
Environment=FW_AGENT_TOKEN=$TOKEN
Restart=always
RestartSec=3
StandardOutput=append:$WORKDIR/agent.log
StandardError=append:$WORKDIR/agent.log

[Install]
WantedBy=multi-user.target
EOF
  chmod 600 "$WORKDIR/fireworks-agent.service"
  $SUDO cp "$WORKDIR/fireworks-agent.service" /etc/systemd/system/fireworks-agent.service
  # unit 内含明文 token：收紧为仅 root 可读，防节点本地用户窥探
  $SUDO chmod 600 /etc/systemd/system/fireworks-agent.service
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable fireworks-agent.service
  $SUDO systemctl restart fireworks-agent.service
else
  # ---- 用户态：systemd --user 服务 + enable-linger 开机自启（无需 root）----
  # 注册到用户管理器并启用 linger：节点重启后用户管理器在无登录会话时
  # 也会启动，unit 随即拉起 agent（保活机制与系统单元一致，均为 Restart=always）。
  # 先停掉可能存在的旧实例（含 systemd --user 服务与历史遗留进程，避免抢端口）
  systemctl --user stop fireworks-agent.service 2>/dev/null || true
  systemctl --user disable fireworks-agent.service 2>/dev/null || true
  pkill -f "uvicorn main:app.*$AGENT_PORT" 2>/dev/null || true
  sleep 1
  cd "$WORKDIR"
  # token 经 0600 配置文件注入：不落入命令行/argv（对 `ps` 与其它用户不可见），
  # 进程从 FW_AGENT_TOKEN 环境变量读取（systemd --user 走 EnvironmentFile，语义一致）
  printf 'FW_AGENT_TOKEN=%s\n' "$TOKEN" > "$WORKDIR/token.env"
  chmod 600 "$WORKDIR/token.env"
  # 用户管理器可用性探测：systemctl 存在且用户管理器的私有 socket 在
  #（SSH 会话内 XDG_RUNTIME_DIR 已就绪即满足）；不可用则部署失败（见 else）。
  if command -v systemctl >/dev/null 2>&1 \
      && [ -S "/run/user/$UID/systemd/private" ]; then
    # 仅引用路径与端口，token 经 EnvironmentFile 单独注入，unit 文件不含明文
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/fireworks-agent.service" <<EOF
[Unit]
Description=Fireworks Agent

[Service]
Type=simple
ExecStart=$VENV/bin/uvicorn main:app --host 0.0.0.0 --port $AGENT_PORT
WorkingDirectory=$WORKDIR
EnvironmentFile=$WORKDIR/token.env
Environment=FW_AGENT_PORT=$AGENT_PORT
Environment=FW_AGENT_WORKDIR=$WORKDIR/work
Restart=always
RestartSec=3
StandardOutput=append:$WORKDIR/agent.log
StandardError=append:$WORKDIR/agent.log

[Install]
WantedBy=default.target
EOF
    chmod 600 "$HOME/.config/systemd/user/fireworks-agent.service"
    systemctl --user daemon-reload
    if loginctl enable-linger 2>/dev/null; then
      echo "[deploy] 已 enable-linger：节点重启后 Agent 将自动启动"
    else
      echo "[deploy] 错误：enable-linger 失败，节点重启后 Agent 将无法自动启动" >&2
      echo "[deploy] 请先以 root 执行: loginctl enable-linger $(id -un)，再重新部署" >&2
      exit 1
    fi
    systemctl --user enable fireworks-agent.service
    systemctl --user restart fireworks-agent.service
    unset FW_AGENT_TOKEN
    echo "[deploy] systemd --user 已启动服务 fireworks-agent.service"
  else
    echo "[deploy] 错误：未检测到 systemd 用户管理器（无法实现开机自启）" >&2
    echo "[deploy] Agent 仅支持 systemd 保活（崩溃自拉起 + 开机自启）。请任选其一后重新部署：" >&2
    echo "[deploy]   1) 为部署用户配置免密 sudo，或改用 root 部署（systemd 系统服务）；" >&2
    echo "[deploy]   2) 让该用户可启动 systemd --user 会话（SSH 登录即自动建立，必要时" >&2
    echo "[deploy]      以 root 执行: loginctl enable-linger $(id -un)）。" >&2
    exit 1
  fi
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
