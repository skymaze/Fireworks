# 🎆 Fireworks — DGX Spark 集群管理工具

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) · [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md)

面向 NVIDIA DGX Spark（GB10）集群的 Web 管理工具，覆盖**节点、集群、模型、任务、配方**五大能力：

- **节点**：添加节点即 SSH 自动部署 Agent（安装/连通验证失败明确报错并回滚），实时监控（CPU / GPU / 温度 / 统一内存 / 硬盘 / 网络）+ `nvidia-smi`
- **集群**：自动探测四条 RoCE rail 的物理链路、现有网段与 IP 占用，组成集群并事务化配置高速网络，一键网络测试（ping / iperf3 / perftest）
- **模型 / 镜像**：控制平面只下载一份；镜像经管理网发送 head 后，由 worker Agent 通过规划的高速 IP 并行直拉，不依赖节点间 SSH/rsync，并展示逐节点进度与速度
- **任务**：容器化运行（docker compose），发布时**按节点指定 head/worker 与 rank**，同一集群可同时运行多个任务，各自不同角色；支持发布 / 暂停 / 继续 / 停止 / 删除 / 日志 / 健康检查
- **配方**：任务配置模板，变量自动填充（共享 / 节点 / 用户三类源），发布向导一条龙
- **总览**：集群与节点物理拓扑、在线节点 GPU 聚合，以及 vLLM 探针 tok/s 趋势、峰值、TTFT P95、KV Cache 与并发基准峰值；指标经 WebSocket 增量更新，离开页面即停止总览请求

已在 2 台 / 4 台 DGX Spark 真机完成端到端验证。

## 快速开始

### 1. 前置条件

- **Docker**（含 Compose v2）：Windows / macOS 装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，Linux 装 Docker Engine
- 检查：`docker compose version`（能打印版本号即可）

### 2. 选择镜像源（二选一）

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

### 3. 初始化并登录

1. 浏览器打开 **http://localhost:3000**
2. 首次访问出现「初始化」页 → 创建**管理员账号**
3. 用该账号登录，进入控制台 🎉

启动成功，接下来按「使用流程」接入真实节点。

### 常见问题

| 现象 | 解决 |
|---|---|
| 端口被占用 | 把配置文件里 `"3000:3000"` 的宿主端口改掉，如 `"8080:3000"`，再访问 `localhost:8080` |
| 登录后立刻跳回登录页 / 登录态丢失 | 是纯 HTTP 却没带 `COOKIE_SECURE=0`，重跑上面命令即可 |
| 想在局域网其它电脑访问 | 端口已对局域网开放，直接用这台机器的局域网 IP 访问；想收紧暴露面可把 compose 端口改为 `管理网IP:8000:8000` |
| 拉镜像慢 / `pull access denied` | 确认选对了源文件（国内选 cn）；镜像为公开仓库、匿名可拉 |

## 使用流程

管理真实集群按此顺序（各页表单内都有提示）：

1. **添加节点（自动部署 Agent）**：`节点 → 添加节点`，填 IP 与 SSH 凭据并保存——控制平面立即经 SSH 上传代码与**离线依赖包**，节点自动安装并以 systemd 托管（开机自启，无需节点访问 PyPI），随后验证连通；**部署与验证通过才算添加成功**，任一失败均明确报错并自动回滚（卸载 Agent + 移除节点，不残留半成品节点）
2. **（可选）重新部署 Agent**：未上线（offline / unknown / error）节点可点列表页「重新部署 Agent」修复或重装
3. **创建集群**：`集群 → 创建集群`，勾选成员节点；WebUI 自动检测四个高速口的载波、跨节点二层连通、现有网段和计划 IP 占用。一致配置会直接复用，混合网段会推荐空闲网段并统一重配；**全部验证通过才创建、失败自动回滚**
4. **添加成员**：在集群详情页点击「添加节点」；WebUI 自动分配/避让 IP 槽位，先做同样的物理链路和占用预检，再配置并执行新旧节点双向验证，无需手写脚本或 SSH 操作
5. **配置配方源**：`配方商店 → 添加配方源`，只需填写仓库地址；系统自动读取远端分支并选择默认分支。可在「源设置」中切换分支并自动重同步，或删除源及目录镜像（不影响已安装配方）
6. **发布任务**：`任务 → 发布任务`：选配方 → 选集群 → 指定 head/worker 与各节点 rank（head 固定 rank 0）→ 配置变量 → 预览 → 发布
7. **日常管理**：日志 / 暂停 / 继续 / 停止 / 删除、健康检查、模型与镜像分发状态。镜像页只保留一个获取入口，默认选择首节点和其余节点，过程自动完成 registry 拉取、归档、分发、校验与加载

不同节点原有高速网 IP 可以处于不同网段；只要对应 rail 位于同一二层交换网络，主动 ARP 探测仍能确认物理互联并由 WebUI 统一重配。完整的拓扑要求、地址规划、创建/加节点状态机、回滚边界与错误处理见 [高速网络自动化说明](docs/networking.md)。

> 想改代码？本地开发（后端 / 前端 / 测试）见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 生产部署

面向局域网内部工具：域名与 TLS 交给你的反向代理（nginx / haproxy / Caddy）终结，把站点（含 `/api/ws/events` WebSocket）反代到前端 `:3000`。示例见 `deploy/nginx-fireworks.conf.example`，HTTPS 场景设 `COOKIE_SECURE=1`。

用**预构建镜像**直接部署（中国大陆选 `docker-compose.prod.cn.yml`，国际选 `docker-compose.prod.yml`），默认使用 `latest`；有正式发布版本后，可用 `FW_IMAGE_TAG` 固定到对应 tag：

```bash
# 国际版示例；中国大陆把文件名换成 docker-compose.prod.cn.yml
docker compose -f docker-compose.prod.yml up -d   # 默认拉取 latest
```

- **端口**：后端 `:8000` 与前端 `:3000` 默认对局域网开放，安全依赖鉴权（Web 登录会话 + Agent 每节点 token）；需要收紧时把 compose 端口改为 `管理网IP:8000:8000`
- **存储**：默认两组命名卷 `fireworks-db`（SQLite + 审计日志，建议 SSD）与 `fireworks-cache`（模型 / 镜像缓存，建议 HDD）；精确落盘见 prod 文件注释

## 架构

```
┌────────────── 控制平面（Docker Compose）──────────────┐
│  Nuxt 4 前端 (3000) ──/api 代理──►  FastAPI 后端 (8000) │
│                                     SQLite 指标库       │
└──────────────────────┬─────────────────────────────────┘
                 SSH 自动部署 │  REST (9000)
       ┌───────────────┼──────────────────┐
       ▼               ▼                  ▼
   Agent #节点       Agent #节点        Agent #节点
   （指标采集 / docker compose / 网络测试；head/worker 由每次任务指定）
```

链路：Nuxt 前端 → FastAPI 控制平面 → 每节点一个轻量 Agent（添加节点时 SSH 自动部署，采集指标、运行容器任务、执行网络互测）。

## 目录结构

```
├── docker-compose.yml          # 开发：本地构建控制平面（后端 + 前端）
├── docker-compose.prod.yml     # 生产·国际版（GHCR 预构建镜像）
├── docker-compose.prod.cn.yml  # 生产·中国大陆版（阿里云预构建镜像）
├── .github/workflows/          # CI：validate（typecheck+单测）与 release（发布时推送镜像）
├── deploy/nginx-fireworks.conf.example  # 反向代理示例（TLS + WebSocket）
├── docs/networking.md         # 高速网络探测、配置、验证与排障
├── docs/image-transfer.md     # 镜像拉取、Agent 高速直传、进度与失败恢复
├── agent/                      # 节点 Agent：单文件 FastAPI 服务 + 部署脚本（离线装依赖）
├── backend/app/                # FastAPI 控制平面（routers / services / 配方源初始化）
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
