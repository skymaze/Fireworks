# 🎆 Fireworks — DGX Spark 集群管理工具

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) · [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md)

面向 NVIDIA DGX Spark（GB10）集群的 Web 管理工具，覆盖**节点、集群、模型、任务、配方**五大能力：

- **节点**：SSH 一键部署 Agent，实时监控（CPU / GPU / 温度 / 统一内存 / 硬盘 / 网络）+ `nvidia-smi`
- **集群**：组成集群并自动配置 RoCE 高速网络，一键网络测试（iperf3 / perftest）
- **模型**：接入 Hugging Face 管理式下载——控制平面下载 → 经管理网发 head → RoCE 同步各 worker，避免逐节点重复下载
- **任务**：容器化运行（docker compose），发布 / 暂停 / 继续 / 停止 / 删除 / 日志 / 健康检查
- **配方**：任务配置模板，变量自动填充（集群 / 节点 / 用户三类源），发布向导一条龙

已在 2 台 / 4 台 DGX Spark 真机完成端到端验证。

## 快速开始（新手 ≤ 5 分钟）

> 用**预构建镜像**、不本地构建代码，只需一台装了 Docker 的机器。

### 1️⃣ 前置条件

- **Docker**（含 Compose v2）：Windows / macOS 装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，Linux 装 Docker Engine
- 检查：`docker compose version`（能打印版本号即可）

### 2️⃣ 选择镜像源（按你的网络环境二选一）

| | 🇨🇳 中国大陆 | 🌍 国际 / 海外 |
|---|---|---|
| 镜像源 | 阿里云（国内下载快） | GitHub 容器仓库（GHCR） |
| 配置文件 | `docker-compose.prod.cn.yml` | `docker-compose.prod.yml` |

**中国大陆**

```bash
docker compose -f docker-compose.prod.cn.yml pull
COOKIE_SECURE=0 docker compose -f docker-compose.prod.cn.yml up -d
```

**国际 / 海外**

```bash
docker compose -f docker-compose.prod.yml pull
COOKIE_SECURE=0 docker compose -f docker-compose.prod.yml up -d
```

> `COOKIE_SECURE=0` 只用于**本机纯 HTTP 访问**（否则浏览器不保存 HTTPS-only 登录 cookie）。放到 HTTPS 反向代理后面正式部署时去掉它，见「生产部署」。

### 3️⃣ 初始化并登录

1. 浏览器打开 **http://localhost:3000**
2. 首次访问出现「初始化」页 → 创建**管理员账号**
3. 用该账号登录，进入控制台 🎉

**跑起来了！** 想管理真实节点，继续看「使用流程」。

### 常见问题

| 现象 | 解决 |
|---|---|
| 端口被占用 | 把配置文件里 `"3000:3000"` 的宿主端口改掉，如 `"8080:3000"`，再访问 `localhost:8080` |
| 登录后立刻跳回登录页 / 登录态丢失 | 是纯 HTTP 却没带 `COOKIE_SECURE=0`，重跑上面命令即可 |
| 想在局域网其它电脑访问 | 端口已对局域网开放，直接用这台机器的局域网 IP 访问；想收紧暴露面可把 compose 端口改为 `管理网IP:8000:8000` |
| 拉镜像慢 / `pull access denied` | 确认选对了源文件（国内选 cn）；镜像为公开仓库、匿名可拉 |

## 使用流程

管理真实集群按此顺序（各页表单内都有提示）：

1. **添加节点**：`节点 → 添加节点`，填 IP 与 SSH 凭据
2. **部署 Agent**：列表页「部署 Agent」——控制平面经 SSH 上传代码与**离线依赖包**，节点自动安装并以 systemd 托管（开机自启，无需节点访问 PyPI）
3. **创建集群**：`集群 → 创建集群`，勾选成员节点；自动配置 RoCE 高速网络，**全部验证通过才创建、失败自动回滚**
4. **发布任务**：`任务 → 发布任务`：选配方 → 选集群 → 变量自动填充 → 预览 → 发布
5. **日常管理**：日志 / 暂停 / 继续 / 停止 / 删除、健康检查、模型与镜像分发状态

> 想改代码？本地开发（后端 / 前端 / 测试）见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 生产部署

面向局域网内部工具：域名与 TLS 交给你的反向代理（nginx / haproxy / Caddy）终结，把站点（含 `/api/ws/events` WebSocket）反代到前端 `:3000`。示例见 `deploy/nginx-fireworks.conf.example`，HTTPS 场景设 `COOKIE_SECURE=1`。

**预构建镜像（推荐）**——CI 自动构建，覆盖 amd64 / arm64：

| | 国际（`docker-compose.prod.yml`） | 中国大陆（`docker-compose.prod.cn.yml`） |
|---|---|---|
| 镜像源 | `ghcr.io/<owner>/fireworks-{backend,frontend}` | `registry.cn-shanghai.aliyuncs.com/aixn-public/fireworks-{backend,frontend}` |

```bash
# 发布 v1.2.3（latest 随之更新；main 分支另有 main / sha-<短哈希> 滚动 tag）
FW_IMAGE_TAG=v1.2.3 docker compose -f docker-compose.prod.yml up -d
```

- 镜像源 / 仓库 / 标签覆盖：`IMAGE_REGISTRY`、`IMAGE_OWNER`、`FW_IMAGE_TAG`。**端口（后端 :8000 / 前端 :3000）默认对局域网开放**，安全依赖鉴权（Web 走登录会话、Agent 端点走每节点 token）；需要收紧时手动把 compose 端口改为 `管理网IP:8000:8000`
- 离线或自建镜像：注释掉 compose 的 `image:` 行、放开 `build:` 段改回本地构建
- 存储自动分两组命名卷：`fireworks-db`（SQLite + 审计日志，建议 SSD）、`fireworks-cache`（模型 / 镜像缓存，建议 HDD）；精确落盘示例见 prod 文件注释

## 架构

```
┌────────────── 控制平面（Docker Compose）──────────────┐
│  Nuxt 4 前端 (3000) ──/api 代理──►  FastAPI 后端 (8000) │
│                                     SQLite 指标库       │
└──────────────────────┬─────────────────────────────────┘
                 SSH 一键部署 │  REST (9000)
       ┌───────────────┼──────────────────┐
       ▼               ▼                  ▼
   Agent #head      Agent #worker      Agent #worker
   （指标采集 / docker compose / 网络测试）
```

链路：Nuxt 前端 → FastAPI 控制平面 → 每节点一个轻量 Agent（SSH 一键部署，采集指标、运行容器任务、执行网络互测）。

## 目录结构

```
├── docker-compose.yml          # 开发：本地构建控制平面（后端 + 前端）
├── docker-compose.prod.yml     # 生产·国际版（GHCR 预构建镜像）
├── docker-compose.prod.cn.yml  # 生产·中国大陆版（阿里云预构建镜像）
├── .github/workflows/          # CI：构建多架构预构建镜像并推送 GHCR
├── deploy/nginx-fireworks.conf.example  # 反向代理示例（TLS + WebSocket）
├── agent/                      # 节点 Agent：单文件 FastAPI 服务 + 部署脚本（离线装依赖）
├── backend/app/                # FastAPI 控制平面（routers / services / seed 配方）
└── frontend/app/               # Nuxt 4 + Nuxt UI v4 前端（pages / server API 代理）
```

## 环境变量

后端控制平面（Compose 中可覆盖）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:////data/db/fireworks.db` | 数据库路径 |
| `COOKIE_SECURE` | 空 | `1` 时登录 cookie 加 Secure（HTTPS 部署开启） |
| `SESSION_TTL_HOURS` | `168` | 登录会话有效期（小时），到期需重新登录 |
| `CORS_ORIGINS` | `http://localhost:3000` | 允许跨域来源（逗号分隔），同源部署基本不参与 |
| `METRIC_POLL_INTERVAL` | `5` | 指标轮询间隔（秒） |
| `METRIC_RETENTION_HOURS` | `24` | 指标保留时长（小时） |
| `AGENT_PORT` / `AGENT_DEPLOY_DIR` | `9000` / `/opt/fireworks-agent` | Agent 监听端口 / 安装目录 |
| `API_PROXY_TARGET` | `http://backend:8000` | 前端 /api 代理目标 |

> Agent 回拉模型 / 镜像的地址无需配置：自动从「下发请求来源 IP」推断控制端地址。

## 安全

- **登录**：单一管理员账号，首次访问「初始化」页创建；所有接口（含 WebSocket）要求登录会话（HttpOnly cookie，可过期 / 登出 / 改密），登录失败按 IP 限速，关键操作写审计日志
- **节点鉴权**：每个节点独立 token（部署即轮换，Bearer 鉴权、fail-closed），任一节点凭证泄露不影响其他节点
- **依赖隔离**：Agent 依赖由控制平面预下载离线包，节点零 PyPI
- **已知限制**：节点 SSH 凭据 / HuggingFace Token 明文存库；控制平面↔Agent 为明文 HTTP（有 token 认证）。建议部署在可信内网，浏览器入口走 HTTPS 反向代理
- 任务以你配置的镜像在节点运行容器，请仅使用可信镜像

## 测试

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest backend/tests        # 后端单测
cd frontend && npm install && npm run build      # 前端构建校验
```
