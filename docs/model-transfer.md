# 模型获取与节点分发

模型管理通过 WebUI 完成 Hugging Face 下载、控制平面缓存和集群分发。默认选择第一台节点作为 head，并选择其余节点作为 worker；用户可修改节点，也可启用“仅缓存到管理平面”。整个流程不需要节点间 SSH 互信或手工执行 rsync。

head 和 worker 角色互斥，同一个 worker 不能重复选择；仅选择 worker 而未指定 head 的请求会被拒绝，避免生成无法执行的分发任务。

## 状态机

1. `downloading`：控制平面读取指定 revision 的仓库 manifest，以多连接 Range 分片下载到 Hugging Face 标准缓存布局。LFS blob 使用 SHA-256 校验，普通 blob 使用 Git blob SHA-1 校验；`.part.N` 支持断点恢复。
2. `sending`：head Agent 经管理网并发回拉 `blobs`、`snapshots`、`refs` 和 `trees`。已有文件只有大小和内容摘要都匹配才会跳过，避免等长损坏文件被误判为完成。
3. `syncing`：控制平面优先从集群 `network_plan` 取得 head 的权威高速 IP。head Agent 为单个仓库生成包含普通文件、symlink、大小与摘要的 manifest，并签发短期只读令牌；所有 worker Agent 并行从 head HTTP 文件流回拉。
4. `completed`：所有目标节点的文件均完成校验并按原始 Hugging Face symlink 布局落盘。

模型不打包成 tar：这能避免额外的临时磁盘、打包 I/O 和整包失败重传，并保留 Hugging Face blob 去重及单文件断点续传能力。每个 worker 内限制文件并发度，避免高速网络下过量并发造成磁盘争用。

## 进度和恢复

- 控制平面到 head 的多个文件进度按任务实时聚合，不会因大文件未完成而长期停在 0。
- WebUI 展示 worker 的独立进度条、当前文件、聚合速度和 ETA；WebSocket 断开时仍有状态轮询兜底。
- 暂停或取消 `syncing` 任务时，控制平面通知所有 worker Agent 停止后台任务并保留 `.part`，并等待旧任务确认退出后才允许新的 fetch 写入同一文件。继续或重试会重新签发令牌并从断点恢复。
- 后端重启后会优先接管 Agent 上仍在运行或已经完成的 worker 子任务，不重复传输；完成/失败记录从结束时刻起保留一小时。若 Agent 子任务已丢失，则使用新令牌从 `.part` 自动续传。不可达等错误会标明具体节点和高速源地址。

## 网络与安全

head 地址选择顺序是：集群网络规划分配的高速 IP、Agent 上报的 RoCEv2 IP、管理 IP。没有高速网络规划时仍可通过管理网完成，只是速度受管理网限制。

节点只接受私有、回环或链路本地 IP 上的 HTTP 模型共享地址；短期令牌绑定单次共享且最长 24 小时，不暴露 Agent 长期管理 token。开始传输前会检查 `model_peer_transfer_v1` capability；缺失能力时任务直接失败并提示重新部署 Agent，不在传输过程中隐式修改节点。

节点间传输不执行 SSH 加密、不依赖 host key，也不调用 rsync。SSH 只保留给控制平面部署 Agent 和配置高速网络使用。
