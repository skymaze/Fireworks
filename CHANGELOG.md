# Changelog

本文件记录 Fireworks 的用户可见变更。版本号遵循 [Semantic Versioning](https://semver.org/)。

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
