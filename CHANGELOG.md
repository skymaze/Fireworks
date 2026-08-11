# Changelog

本文件记录 Fireworks 的用户可见变更。版本号遵循 [Semantic Versioning](https://semver.org/)。

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

[0.1.0]: https://github.com/skymaze/Fireworks/releases/tag/v0.1.0
