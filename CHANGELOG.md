# Changelog

本文件记录 Fireworks 的用户可见变更。版本号遵循 [Semantic Versioning](https://semver.org/)。

## [0.5.0] - 2026-08-27

### Added

- 任务健康检查改为**以 docker compose 容器 healthcheck 为准**：配方在 compose 声明 `healthcheck:` 即生效（Agent 暴露容器 Health；healthy→运行中、unhealthy→错误，健康类错误可随容器转 healthy 自动恢复）；未声明时健康状态显示「未配置」，不做判定——不再由控制面写死端口/探测路径。
- 任务详情容器表新增「健康」列（健康 / 异常 / 检查中 / 未配置，中英文）。

### Changed

- 移除健康检查对 `VLLM_PORT + /v1/models` 的写死探测与相关兜底代码（清理复杂逻辑）。

### Release notes

- 控制面升级到 `0.5.0`：启动时自动迁移为 `task_nodes` 添加 `container_health` 列（幂等，旧数据保留）；节点 Agent 建议随升（旧 Agent 无 Health 字段时健康显示「未配置」，仍兼容）。
- 无任务语义与数据格式破坏性变化；升级前建议备份 `fireworks-db` 卷。
## [0.4.0] - 2026-08-27

### Added

- **任务停止后可「启动」、运行中可「重启」**：停止改为 `docker compose stop`（保留容器，GPU 释放），之后「启动」用 `docker compose start` 复用容器一键拉起（容器被清理时自动回退 `compose up` 重建）；`running` 任务「重启」用 `docker compose restart`，均不重建容器，启动/重启后自动补发 vLLM 健康检查。
- Agent 新增 `POST /api/compose/action`（stop/start/restart）；任务详情页与列表页新增「启动/重启」按钮（中英文文案）。

### Changed

- **「停止」语义变更**：由 `compose down`（删除容器）改为 `compose stop`（保留 exited 容器供「启动」复用）；彻底释放磁盘请用「删除」。

### Release notes

- **控制平面与节点 Agent 均需升级到 `0.4.0`：升级控制平面后请重新部署各节点 Agent**（旧版 Agent 无 `/api/compose/action`）。大版本号即「需重部署 Agent」标识（同 v0.2.0 机制）。
- 无数据格式与任务语义的破坏性变化，无需迁移；升级前建议备份 `fireworks-db` 卷。

## [0.3.3] - 2026-08-26

### Fixed

- 修复镜像拉取在 registry 鉴权持续失败（401）时无限重试、任务卡死 `pulling` 并阻塞该镜像后续传输的问题；重认证最多重试 5 次后明确失败。
- 修复模型下载设置变更重启时，若旧下载线程未在 120 秒内退出会遗留「任务停留在 `downloading` 且无活动线程」的问题：新增看门狗，旧线程退出后自动按新设置断点续传重启。
- 修复任务并发操作竞态：暂停/继续/停止/删除同一任务现按状态机串行化并校验合法转移（对已停止任务「继续」返回 409，不再制造「无容器的 running」卡死态）；部署期间用户发出的停止/暂停不再被发布流程覆盖为 `running`；容器操作全部失败时任务置 `error` 而非虚报成功。
- 修复推理统计在采样点时间戳相同/倒流时产生百万级虚假 `tok/s` 峰值（跳过无效区间并对极小间隔钳制分母）。
- 修复中断下载残留的分片（`*.part.N`）被误计入缓存大小、甚至被当作完整模型文件发送到节点的问题。
- 修复 SSH 远端命令在进程存活但静默时无限占用线程的问题：命令执行增加总超时（到点终止远端进程并明确报错）。
- 删除进行中的下载/传输记录前先取消后台调度（防孤儿线程双写缓存）；删除仍被下载/分发占用中的模型缓存返回 409（`MODEL_BUSY`）。
- 任务日志接口 `tail` 参数钳制在 1–5000 行，防止超大回放占用内存。
- 安全加固：模型 `revision` 增加字符集与路径越级校验（拒绝 `..` 等写盘越界）；节点 SSH 用户名 / IP 增加入口校验（拒绝 shell 元字符），Agent 卸载脚本对非法用户名在 SSH 之前拒绝；head Agent 返回的高速共享路径/令牌增加校验（禁止 userinfo 注入把 worker 拉取重定向到外部主机）。

### Release notes

- 控制平面升级到 `0.3.3`：安全加固 + 健壮性修复；API 响应结构、任务语义不变，**升级无需迁移**（SQLite 无 schema 变化）。节点 Agent 仅版本号同步、无功能改动，**无需重部署**。
- 需注意的新行为（均为防御性拒绝，正常流程不变）：对已 `stopped` 任务执行「继续」会返回 409；删除仍在 `downloading/sending/syncing` 的任务记录会先自动取消后台调度；删除仍被进行中任务占用的模型缓存会返回 409（请先取消任务）。
- 升级前建议备份 `fireworks-db` 卷。

## [0.3.2] - 2026-08-19

### Fixed

- 修复发布任务页「模型缓存状态」卡片不显示：前端此前硬编码按 `DSPARK_MODEL` 变量识别模型仓库，配方模型变量使用其它键名（DeepSeek b12x `SPARK_MODEL`、GLM `GLM52_MODEL_PATH`、Qwen `SGLANG_MODEL` 等）时发布页解析不到模型，节点缓存状态卡片、页内「发送模型」与发布前模型就绪解锁整条链路不渲染、不生效。现与后端模型保障（`tasks.py` 的 `picker=="model"` 动态取键）及镜像侧（`picker=="image"`）一致，按配方 `picker=="model"` 变量动态取键。

### Release notes

- 控制平面升级到 `0.3.2`：发布页模型缓存状态卡片修复；无数据格式与任务语义变化，升级无需迁移。节点 Agent 仅版本号同步、无功能改动，无需重部署。

## [0.3.1] - 2026-08-19

### Fixed

- 修复平台发布时的"模型保障"链路对模型变量键名硬编码 `DSPARK_MODEL`：配方模型变量使用其它键名（如新增 DeepSeek b12x 配方的 `SPARK_MODEL`、GLM 的 `GLM52_MODEL_PATH`）时，发布会静默跳过「模型卡片/清单拉取 → 校验 → 下载并分发到节点 → 就绪后强制离线」的保障链路。现改为按配方的 `picker=="model"` 变量动态取键，任意键名均生效。
- 发布时以规范键 `MODEL_ID` 记录模型仓库（与 `send_model` 开关解耦），使终止任务「删除已分发模型」、推理统计取模型名对非 `DSPARK_MODEL` 键的配方同样生效；老配方数据的 `DSPARK_MODEL` 键兼容回退。

### Release notes

- 控制平面升级到 `0.3.1`：模型保障链路修复；无数据格式与任务语义变化，升级无需迁移。节点 Agent 无改动，无需重部署。

## [0.3.0] - 2026-08-18

### Added

- **添加节点时执行初始优化**（新功能）：新增节点并部署 Agent 成功后（默认开启，可关闭），SSH 以 root 依次执行 4 项优化并调度重启——关闭 Wi-Fi/蓝牙、关闭图形界面、授予 docker 权限、禁用 swap；整体 best-effort，单项失败/取不到 root 不阻断添加节点，结果写入节点 `optimize_result`（前端节点详情展示）。

### Fixed

- 修复镜像向节点分发在中途断流（管理网/RoCE 抖动、代理截断）时直接失败：Agent 归档拉取改为保留 `.part` 分片并按 Range 断点续传（有界重试），416 在 urllib 实际抛出的 HTTPError 层处理；此前任一阶段中断都会让整个分发任务失败。
- 修复归档已缓存时仍强制联网检查 registry 导致无法分发：registry 不可达但只要控制平面已有该镜像归档，分发照常进行（离线/受限网络可用）；仅无缓存且查不到镜像时才报错。
- 修复镜像分发「假完成」：Agent `image_load` 此前仅凭 `.loaded-<digest>` 标记跳过加载，镜像被 docker 清理/更换后标记残留，任务显示成功但节点 docker 中并无该镜像。改为「标记存在 **且** docker 中该镜像真实存在」才跳过，否则真实执行 `docker load`；已加载的节点仍幂等跳过、不重复加载。

### Changed

- 镜像分发性能优化（参考模型分发）：registry 各层并行下载（默认 4）、归档组装阶段各层并行解压后再按 manifest 顺序写 tar（输出指纹不变）、head 与 worker 的 `docker load` 并行执行（总耗时从「各节点求和」降为「最慢节点」）。
- 重复分发不再整份重算归档/层指纹：控制平面与 Agent 均以 size+mtime 标记（sidecar / `.verified`）判断「已存在且完整」，直接跳过下载且不重读文件；标记失效才回退全量校验。
- 并行度可调：`IMAGE_PULL_LAYER_WORKERS` / `IMAGE_PACKING_WORKERS`。

### Release notes

- 控制平面与 Agent 统一升级到 `0.3.0`。分发的断流续传与并行化、节点初始优化均不改变既有数据格式与任务语义，升级后无需额外迁移。
- 本轮含 dev 提测构建：`FW_IMAGE_TAG=dev` 使用阿里云源 `registry.cn-shanghai.aliyuncs.com/aixn-public/fireworks-*`。

## [0.2.1] - 2026-08-12

### Changed

- 创建集群不再自动复用节点现有高速网络，始终按界面提供的网段规划并配置（弹窗自动填入空闲网段，可手动修改）；网段与现网冲突时提示并自动建议可用网段，移除独立的「网络预检」按钮。
- 删除集群时一并清理其下已结束任务及关联历史数据（task_nodes / 推理统计 / 压测记录），不再保留失去集群引用的孤儿任务；未停止的任务仍须先停止。

### Fixed

- 修复删除集群后新建集群出现上个集群数据的问题：`clusters`、`nodes`、`recipes` 主键均启用 AUTOINCREMENT（主键单调不复用），遗留引用不再串到后创建的数据；启动时对旧库幂等迁移，并自愈上次升级中断残留的 `*_legacy` 表（数据自动搬回）。
- 修复镜像传输 `docker load` 失败 `archive/tar: invalid tar header`：docker-archive 组装支持 zstd 压缩层（buildah/podman 构建的镜像），解压为 plain tar 后加载。
- 镜像传输链路加固：blob 下载 416 循环保护、Agent 端 416 断点续传兜底、head 回拉结果校验，发布前镜像检查错误提示区分 Agent 版本过旧。

### Release notes

- 控制平面与 Agent 统一升级到 `0.2.1`。升级控制平面后建议重新部署节点 Agent，以保持能力对齐。
- v0.2.1 启动时会对 `clusters` / `nodes` / `recipes` 执行主键单调性迁移并自愈中断残留（秒级，数据保留）；曾运行含迁移缺陷开发版的部署，升级后「节点集群数据丢失」会自动恢复。升级前仍建议备份 `fireworks-db` 卷。

## [0.2.0] - 2026-08-12

### Added

- 节点列表和详情页始终提供 Agent 安装操作；根据节点与控制平面的版本关系显示“升级 Agent”“降级 Agent”或“重装 Agent”。
- 新增统一的推理窗口统计接口，支持时间范围、任务过滤、完整摘要与有界图表时间桶。
- 前端新增推理统计纯逻辑单测，并纳入日常 CI。

### Changed

- vLLM 推理统计改为被动读取 `/metrics` 的真实累计计数器、KV gauge 与延迟直方图，不再发送会扰动服务的合成推理请求。
- 推理监控支持最近 1 小时 / 24 小时窗口；后端对完整累计快照按任务差分，展示 Decode / Prefill tok/s、请求数、TTFT P95、E2E、KV Cache、窗口合计与趋势图。
- 推理区间差分指标改用柱状图展示；Token 图统一使用 Decode / Prefill tok/s，请求数按自身单位独立成图，KV Cache 时点指标保留折线。
- 推理统计改为按窗口 HTTP 查询；总览接口仅提供资源拓扑与 GPU 聚合，Decode / Prefill 窗口摘要由专用统计接口计算。
- 推理图表改为服务端时间桶聚合；点数参数仅控制图表分辨率，完整源区间仍参与总量、平均、峰值和延迟直方图计算；多任务分别获得点数预算。
- 统计卡片补充聚合前计算的请求峰值（req/s），并以次要信息配对展示 Prefill 窗口平均、Prefill 合计和窗口总请求数。
- 窗口合计卡片明确标注为 Decode；KV Cache 改为展示原始采样的窗口峰值，图表桶内保留最大值，并在空闲压缩时保留 gauge 变化，避免短时占用被后续空闲 0 覆盖。
- 活跃期保留逐点快照，空闲期滚动维护紧邻流量的边界快照，兼顾吞吐时间分母准确性与数据库体积。

### Fixed

- 修复无流量时最后一个吞吐样本不会按真实时间过期、历史窗口无法自然裁剪的问题。
- 修复旧式全局 `limit` 等距抽样会丢失任务首尾累计快照、低流量任务和窗口统计精度的问题。
- 启动时幂等清理旧版派生格式的推理统计样本，避免新旧数据混合计算。
- 调整发布清单生成方式，兼容不接受 OCI attestation manifest 的阿里云 ACR；GHCR 仍保留独立 provenance。
- 修复同时部署多个节点 Agent 时，后一次操作会清除前一行 loading 状态的问题。

### Release notes

- Fireworks backend、frontend 与 Agent 统一升级到 `0.2.0`；升级控制平面后必须重新部署节点 Agent，旧版 Agent 不提供新的 `/api/inference/stats` 接口。
- 本版本没有表字段或业务数据迁移，可直接复用 v0.1.1 的 `fireworks-db`；启动时会自动补建推理查询索引，并删除无法用于新统计链路的旧格式推理样本，升级前仍建议备份。

## [0.1.1] - 2026-08-11

### Changed

- 模型搜索入口移至页面 Header 弹窗，搜索结果和直接下载均改为单击后立即下载到控制平面，避免搜索结果挤压主页面布局。
- 下载完成的模型和镜像统一从缓存/归档列表发起分发；分发弹窗先选择集群，再选择节点，默认全选且首个节点作为 head。
- 模型与镜像分发在后端统一校验节点归属，拒绝未加入集群和跨集群的分发请求。
- 前端将 ECharts 与 zrender 拆为独立缓存 chunk，消除生产构建的大 chunk 警告。

### Fixed

- 修复长时间运行的任务离开详情页再进入时，复用旧实时连接导致容器日志缓存不回放、页面无法查看日志的问题。

### Release notes

- Fireworks backend、frontend 与 Agent 统一使用版本 `0.1.1`。
- 本版本没有数据库结构变更，可直接复用 v0.1.0 的 `fireworks-db`；升级前仍建议备份。

## [0.1.0] - 2026-08-11

Fireworks 首个公开版本，面向 NVIDIA DGX Spark（GB10）集群的部署、网络配置、模型/镜像分发与推理任务管理。

### Added

- 节点 SSH 接入、Agent 离线部署、鉴权、状态采集与 WebSocket 实时通道。
- 四 rail RoCE 网络探测、IP 规划、Netplan 事务化配置、冲突检测与失败回滚。
- 集群级 ping、iperf3 与 RDMA perftest 网络测试。
- 模型和镜像只下载一次，经 head 节点及高速网络并行分发到 worker。
- 配方源发现、浅克隆同步、目录浏览、安装和中断同步恢复。
- 任务发布、暂停、继续、停止、删除、健康检查、实时日志和性能压测。
- 集群拓扑、GPU 汇总、推理吞吐、TTFT、KV Cache 与并发基准总览。
- 中英文界面、登录会话、审计日志、Docker Compose 生产部署，以及同时推送 GHCR/阿里云 ACR 的多架构镜像流水线。

### Changed

- 创建集群时网络预检改为可选操作；正式创建执行一次权威校验，多节点检测、配置与验证并行进行。
- 模型与镜像分发不再依赖节点间 SSH/rsync，改由带作用域令牌的 Agent 高速直传。
- 发布任务前强制刷新节点信息，并在数据库事务内原子预留节点。

### Fixed

- 集群创建完成后刷新节点网络/GID 快照，避免数据库保留旧网络配置。
- 修复任务删除与探针、压测晚到结果之间的竞态及 SQLite 孤儿数据问题。
- 修复日志流快速重订阅、旧 `log_end` 干扰新流，以及进度日志结束后普通日志持续覆盖末行的问题。
- 修复配方源因服务中断永久停留在 `syncing`、无法手动恢复的问题。

### Release notes

- Fireworks backend、frontend 与 Agent 统一使用版本 `0.1.0`。
- 首次发布直接使用最终数据库模型建库，不支持把开发阶段数据库作为正式数据升级。

[0.2.0]: https://github.com/skymaze/Fireworks/releases/tag/v0.2.0
[0.1.1]: https://github.com/skymaze/Fireworks/releases/tag/v0.1.1
[0.1.0]: https://github.com/skymaze/Fireworks/releases/tag/v0.1.0
