# 🎆 Fireworks — DGX Spark 集群管理工具

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) · [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md)

面向 NVIDIA DGX Spark（GB10）集群的 Web 管理工具，覆盖**节点、集群、模型、任务、配方**五大能力：

- **节点**：添加/删除/详情；硬件配置、温度、CPU/GPU 使用率、统一内存、硬盘、网络六组实时图表，以及原始 `nvidia-smi` 输出
- **集群**：把节点组成集群，配置 head/worker 角色与 RoCE 高速网络参数，节点间一键运行网络测试（iperf3 / ib_write_bw / ib_read_bw / ping）
- **模型**：接入 Hugging Face，搜索/查看模型；**管理式下载**——head 节点经管理网从 HF 下载，完成后经 RoCE 高速网同步到各 worker，避免所有节点同时从互联网下载抢占带宽；发布时自动检查模型缓存（不完整则 409 并自动启动管理下载）
- **任务**：以容器形式运行（docker compose）；查看运行中任务、暂停/继续/停止/删除、查看容器日志
- **配方**：任务配置模板，支持变量化；发布任务时自动填充集群与节点参数（参考配方 [MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark) 的优化版）

## 架构

```
┌────────────── 控制平面（Docker Compose）──────────────┐
│  Nuxt3 前端 (3000)  ──/api 代理──►  FastAPI 后端 (8000) │
│                                     SQLite 指标库       │
└──────────────────────┬─────────────────────────────────┘
                 SSH 一键部署 │  REST (9000)
       ┌───────────────┼──────────────────┐
       ▼               ▼                  ▼
   Agent #head      Agent #worker      Agent #worker
   （指标采集 / docker compose / 网络测试）
```

- **Agent**：每节点一个轻量 Python 服务（宿主机运行），通过 `nvidia-smi`、`/sys/class/thermal`、`psutil`、`docker` CLI 采集指标与执行容器生命周期；**模型职责**：拉取（pull）、同步（rsync）、列表、删除、逐文件完整性校验；**镜像职责**：拉取、docker load、RoCE 同步、状态
- **模型管理流程（与任务解耦）**：模型页搜索/直接下载 HF 模型 → 自研分块下载器在**控制平面后台**完成（Range 多连接、断点续传、git blob SHA-1/LFS sha256 双重校验）→ 经**管理网**发送到 head（agent 反向拉取，GET 流式 + 断点续传）→ head 经 **RoCE 高速计算网**（免密 SSH + rsync；免密由 Agent 部署自动生成密钥、创建集群/加节点时自动配置 head→成员）同步到各 worker。发布任务时可选「是否发送模型」，终止任务时可选「是否删除节点模型」，节点详情页可直接查看/删除节点模型
- **镜像管理流程（方案 A：管理平面分发）**：控制平面拉取镜像（skopeo / Python registry 客户端，强制 linux/arm64，流式落盘 + Range 续传 + 中断重试 + sha256 校验，支持 http/https/socks5 代理）→ 经管理网发送 head → RoCE 同步 worker（失败自动重试 3 次）→ 各节点 docker load（按归档指纹 `.loaded-<digest>` 标记幂等跳过，避免旧版本同名镜像误判已加载）；docker 中已有该 tag（present）即视为发布就绪；归档可单独删除/强制重新拉取最新版本
- **配方变量**：三类变量自动填充
  - `cluster` 源：`MASTER_ADDR`（head 的 RoCE IP）、`MASTER_PORT`、`NODES_TOTAL`（支持 `min` 最少节点数校验）等
  - `node` 源：`NODE_RANK`、`VLLM_HOST_IP`、`NCCL_IB_HCA`、`NCCL_SOCKET_IFNAME`、`NCCL_IB_GID_INDEX`（sysfs 自动解析 RoCEv2 GID index，重启漂移自适应）、`HEADLESS`（worker 自动 `--headless`）等
  - `user` 源：模型、镜像（支持快速选择已下载/已拉取项）、`MAX_MODEL_LEN`、`GPU_MEMORY_UTILIZATION` 等，发布向导填写
- **发布编排**：worker 先起、head 后起（避免 mp-init 竞态，参考配方经验），随后后台轮询 head 的 `/v1/models` 健康检查；容器状态 30s 监控（全部退出自动 stopped，服务就绪自动恢复 running）

## 实测验证

已在 **2 台与 4 台 NVIDIA DGX Spark（GB10）** 真机完成多轮端到端验证，全部操作经 WebUI 完成：

- **Agent 部署**：SSH 一键部署（venv + 离线依赖包安装，无 PyPI 依赖；非 root 自动回退用户态 + systemd/nohup 保活；自动生成 head→worker SSH 免密）
- **硬件 / RoCE 检测**：GB10 GPU、温度、4× 100G HCA、RoCEv2 GID 自动解析；集群高速网络自动配置/验证/回滚（2/3/4 节点实测）
- **容器任务**：worker-first 发布、GPU 直通、健康检查、暂停/继续/停止/日志
- **模型 / 镜像管理式分发**：控制平面下载 → 管理网发送 head → RoCE 同步 worker → docker load / 完整性校验
- **网络测试**：iperf3 / perftest（100G RoCE 满速 ≈ 11–12 GB/s）

验证中发现并修复的典型问题（摘要）：`nvidia-smi` FB Memory N/A 回退统一内存、非 root 用户部署、
perftest 残留进程占端口、任务健康检查守卫一致性、HF trees 元数据与 huggingface_hub 兼容、
worker 缺 `--headless`、镜像大文件代理中断续传、高速网络按 NVIDIA 官方布局（每 PCIe 通道独立 /24）配置、
netplan/IP 规划冲突处理、删除集群的任务与网络保护等。

## 快速开始（Docker Compose）

**前置要求**：
- 控制平面：装有 Docker（含 Compose v2）的主机即可运行
- 节点：可 SSH 登录的 Linux 主机（NVIDIA DGX Spark 最佳，亦可用任意带 NVIDIA GPU 的 Linux 节点）；需 **python3 ≥ 3.10** 与 `docker` CLI（Agent 以 venv + systemd/nohup 运行；依赖离线安装，节点无需访问 PyPI）
- 网络：控制平面与节点位于同一（管理）网络；节点间建有 RoCE 高速网时可启用集群高速网络配置与模型/镜像同步

```bash
docker compose up -d --build
# 前端  http://localhost:3000  （首次访问需先「初始化」创建管理员账号，此后登录使用）
# 后端  http://localhost:8000/docs  （接口均需登录会话，浏览器经 :3000 代理访问）
```

开发模式（本地跑）：

```bash
# 后端
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
DATABASE_URL=sqlite:///./dev.db .venv/bin/uvicorn app.main:app --app-dir backend --reload

# 前端（/api 默认代理到 localhost:8000）
cd frontend && npm install && npm run dev
```

## 测试

```bash
# 后端（需 dev 依赖）
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests

# 前端构建校验
cd frontend && npm install && npm run build
```

## 生产部署

面向局域网内部工具：域名与 TLS 由你现有的反向代理（nginx / haproxy / Caddy / 网关）终结，把站点（含 `/api/ws/events` WebSocket）反代到前端 `:3000` 即可。示例配置见 [`docker-compose.prod.yml`](docker-compose.prod.yml) 与 [`deploy/nginx-fireworks.conf.example`](deploy/nginx-fireworks.conf.example)（含 TLS 终止、WebSocket 升级头与 `X-Forwarded-*` 转发头；配合 `COOKIE_SECURE=1`）。

**预构建镜像（推荐，国际部署）**：后端/前端/Agent 镜像由 CI 构建并推送到 GHCR（`.github/workflows/build-images.yml`），命名 `<registry>/<owner>/fireworks-{backend,frontend,agent}`，同时支持 `linux/amd64` 与 `linux/arm64`。推送 `vX.Y.Z` 标签即发布（`latest` 随之更新），推送 main 分支为滚动构建（`main` / `sha-<短哈希>`）。使用 [`docker-compose.prod.yml`](docker-compose.prod.yml) 部署：

```bash
docker compose -f docker-compose.prod.yml pull
FW_IMAGE_TAG=v1.2.3 docker compose -f docker-compose.prod.yml up -d
```

**中国大陆部署**：使用 [`docker-compose.prod.cn.yml`](docker-compose.prod.cn.yml)（阿里云源 `registry.cn-shanghai.aliyuncs.com/aixn-public/fireworks-*`）。阿里云镜像**本地构建后推送**（先 `docker login registry.cn-shanghai.aliyuncs.com`）：

```bash
# 本地构建并推送三个镜像（agent 需双架构，用 buildx）
docker build -f backend/Dockerfile -t registry.cn-shanghai.aliyuncs.com/aixn-public/fireworks-backend:latest .
docker build -f frontend/Dockerfile -t registry.cn-shanghai.aliyuncs.com/aixn-public/fireworks-frontend:latest frontend
docker buildx build --platform linux/amd64,linux/arm64 -f agent/Dockerfile \
  -t registry.cn-shanghai.aliyuncs.com/aixn-public/fireworks-agent:latest --push .

# 部署
docker compose -f docker-compose.prod.cn.yml pull
FW_IMAGE_TAG=v1.2.3 docker compose -f docker-compose.prod.cn.yml up -d
```

自建 registry 同理：`IMAGE_REGISTRY` / `IMAGE_OWNER` 可覆盖两份 compose 的默认源。

离线或需自研镜像时改回本地构建：把 compose 中两个服务的 `image:` 行注释掉、放开下方 `build:` 段，再执行 `docker compose -f docker-compose.prod.yml build`。

**存储分层（SSD/HDD 分流）**：默认用命名卷 `fireworks-db`（SQLite 数据库 + 审计日志，建议 SSD）与 `fireworks-cache`（模型缓存 + 镜像归档，建议 HDD）——跨平台最兼容（Windows/macOS/Linux 一致，Docker 管理位置与权限）。生产需要精确落盘时，把对应命名卷改成 `driver: local` + bind 固定到目标磁盘（示例见 `docker-compose.prod.yml` 底部注释），例如 Linux `device: /mnt/ssd/fireworks/db`、Windows `device: D:\fireworks\db`。

## 使用流程

1. **添加节点**：`节点 → 添加节点`，填 IP/SSH 信息（密码或私钥）
2. **部署 Agent**：列表页点「部署 Agent」——控制平面经 SSH 上传 Agent 代码与**离线依赖包**（backend 镜像构建时预下载，覆盖 Python 3.10-3.13 × amd64/arm64），节点 venv 离线安装并以 systemd/nohup 运行（需节点可 SSH 登录、python3 ≥ 3.10；会自动生成 SSH ed25519 密钥供集群内免密）
3. **创建集群**：`集群 → 创建集群`，勾选成员节点（**已在其他集群的节点自动禁用，一节点一集群**）；网段自动填入当前可用值（`GET /api/clusters/available-cidr`，10.0.0.0/16 起自动自增，提交时校验冲突则提示并更新）——系统自动配置节点高速网络（4×100G 接口按官方布局分配 plan IP、双向验证）**全部通过才创建，失败自动回滚**；创建后自动配置 head→各成员 SSH 免密（镜像/模型 RoCE 分发依赖；失败仅警告）
4. **添加节点**：集群详情页「添加节点」，默认按集群规划配置高速网络并验证（同接口同网段，node_rank 唯一、失败回滚）
5. **（可选）配置配方**：`配方` 页编辑或新建；内置 DeepSeek-V4-Flash 2x DGX Spark 种子配方（导入外部配方自动补 `entrypoint: []` 等兼容修正，`import_notice` 提示）
6. **发布任务**：`任务 → 发布任务`——选配方 → 选集群 → 选 head/worker → 变量（集群/节点变量已自动填充、多 HCA 注入 NCCL_IB_HCA、head 非 rank0 顶部警告）→ 预览 → 发布
7. **管理任务**：详情页暂停/继续/停止/删除，查看各节点容器状态与日志

## 目录结构

```
├── LICENSE                 # MIT
├── .github/workflows/      # CI：构建多架构镜像推送 GHCR
├── docker-compose.yml      # 控制平面（后端 + 前端）
├── docker-compose.prod.yml # 生产示例·国际版（GHCR 源，中间件终结 TLS 拓扑）
├── docker-compose.prod.cn.yml # 生产示例·中国大陆版（阿里云源）
├── deploy/                 # 中间件反向代理示例（nginx）
├── agent/                  # 节点 Agent（容器化部署到各 DGX Spark）
│   ├── main.py             # 单文件 FastAPI 服务（指标/容器/网络测试）
│   ├── deploy.sh           # 节点部署脚本（venv + systemd/nohup，离线安装依赖）
│   ├── requirements.txt    # 依赖锁定（uvicorn+websockets，全 abi3/纯 Python 可离线打包）
│   └── wheels/             # 离线依赖包（backend 镜像构建时生成，随部署上传，节点零 PyPI）
├── backend/app/            # FastAPI 控制平面
│   ├── routers/            # nodes / clusters / recipes / tasks / overview
│   ├── services/           # agent_client / ssh / deploy / recipe_render / metrics / network_test
│   └── seed.py             # 内置 DeepSeek 种子配方
└── frontend/app/           # Nuxt 3 + Nuxt UI + ECharts
    ├── pages/              # 总览 / 节点 / 集群 / 配方 / 任务
    └── server/routes/api/  # /api 运行时代理到后端
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:////data/fireworks.db` | 数据库路径 |
| `METRIC_POLL_INTERVAL` | `5` | 指标轮询间隔（秒） |
| `METRIC_RETENTION_HOURS` | `24` | 指标保留时长（小时） |
| `AGENT_PORT` | `9000` | Agent 监听端口 |
| `AGENT_DEPLOY_DIR` | `/opt/fireworks-agent` | Agent 安装目录 |
| `API_PROXY_TARGET` | `http://backend:8000` | 前端 /api 代理目标 |
| `SESSION_TTL_HOURS` | `168` | 登录会话有效期（小时），到期需重新登录 |
| `CORS_ORIGINS` | `http://localhost:3000` | 允许跨域来源（逗号分隔）；同源部署基本不参与 |
| `COOKIE_SECURE` | 空 | 设置为 `1` 时登录 cookie 加 `Secure` 标记（HTTPS 部署开启） |

> 节点 Agent 回拉模型/镜像的地址无需配置：Agent 自动从「下发请求来源 IP」推断控制端地址（docker 经宿主机 NAT 亦正确），控制端换机/换 IP 零配置适配。

## 安全说明

- **登录认证**：控制平面已启用单一用户系统。首次部署访问前端显示「初始化」页创建管理员账号；此后所有 API（含 `/ws/events` 实时通道）均要求登录会话，会话存 HttpOnly cookie（可注销/可过期/可改密），登录失败按来源 IP 限速防爆破，关键操作写审计日志
- **节点 Agent 鉴权**：每个节点在「部署 Agent」时生成**独立 token**（部署即轮换），控制平面→Agent 的 HTTP/WS 请求携带该节点自己的 token（Bearer 头），Agent 侧每个端点恒时校验（未配置 token 时拒绝一切请求/fail-closed）；Agent 回拉模型/镜像时以同一 token 反向认证，控制平面按 token 识别节点身份——**任一节点凭证泄露不影响其他节点**。token 明文存于控制平面数据库（需明文回放至请求头），DB 权限即密钥权限
- **Agent 依赖隔离**：节点 Agent 依赖由控制平面预下载（backend 镜像构建时生成离线包），部署时随代码上传、节点离线安装——节点无需访问 PyPI，网络抖动不影响部署
- **实时通道**：WebSocket 经前端 `:3000` 同源代理到后端（`/api/ws/events`），浏览器不直连 `:8000`；`8000` 端口仍发布——节点 Agent 通过管理网回拉模型/镜像文件使用（携带节点 token 认证）
- **已知限制**：节点 SSH 凭据（密码/私钥）与 HuggingFace Token 明文存于控制平面数据库；控制平面↔Agent 为明文 HTTP（有 token 认证、无传输加密）；生产建议将控制平面与节点部署在可信管理网段，浏览器入口经反向代理启用 HTTPS
- 任务发布会以你配置的镜像在节点上运行容器，请仅使用可信镜像

## Roadmap

- [x] 节点管理 + 六组实时图表 + nvidia-smi
- [x] 集群成员/角色管理 + 网络测试（RoCE iperf/perftest）
- [x] 配方变量化 + 内置 DeepSeek 配方 + 发布预览 + 导入/导出
- [x] 任务发布（worker-first）/暂停/继续/停止/日志/健康检查
- [x] 真机端到端验证（2× DGX Spark）
- [ ] 指标长期趋势查询优化、Agent 自动升级
- [ ] 多控制平面高可用、发布历史审计
