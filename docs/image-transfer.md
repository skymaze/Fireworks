# 镜像获取与节点分发

镜像管理通过一个 WebUI 流程完成，不要求用户登录节点、配置节点间 SSH 互信或手工运行脚本。默认把第一台节点作为 head、其余节点作为目标；可修改选择，清空 head 则只缓存到控制平面。

## 状态机

1. `pulling`：控制平面使用唯一的 registry 客户端拉取 linux/arm64 manifest 和 layer。每层流式写入 `.part`，支持 Range 续传、重试、大小和 SHA-256 校验；已验证 layer 可跨任务复用。
2. `packing`：把已验证的压缩 layer 流式组装为 Docker archive。完成后用整个归档的 SHA-256 作为后续传输的一致性标识。
3. `sending`：head Agent 从控制平面经管理网回拉归档，支持断点续传并在完成前同时校验归档大小和 SHA-256。
4. `syncing`：控制平面从集群 `network_plan` 取得 head 的权威高速 IP；每个 worker Agent 使用独立短期令牌，并行从 head Agent 的 HTTP 文件流回拉。该链路为局域网明文 HTTP，不执行 SSH 加密，也不调用 rsync；短期令牌只允许读取指定 digest 且自动过期。
5. `loading`：各节点执行 `docker load`，再次校验归档指纹；同一归档已成功加载时幂等跳过。

WebUI 展示控制端下载、发送 head 的总进度和速度，以及每个 worker 的独立字节进度。实时事件不可用时，5 秒状态轮询仍会恢复当前进度。

开始节点分发前会检查 `image_peer_transfer_v1` capability；缺失能力时任务直接失败并提示重新部署 Agent，不在传输过程中隐式修改节点。

## 网络选择与降级

head 地址优先取集群网络规划中按 `net_index` 分配的高速 IP，因此不依赖 Agent 尚未刷新的硬件缓存；无网络规划时依次回退 Agent 上报的 RoCEv2 IP 和管理 IP。使用管理 IP 时功能仍可完成，但速度受管理网限制。

## 失败恢复与诊断

- registry 中断：保留 layer `.part`，重试时从已写字节继续；服务端不支持 Range 时安全地从头重下。
- 控制端或节点已有损坏缓存：大小或 SHA-256 不匹配时删除损坏文件并重新传输，不会把非空文件误判为完成。
- head→worker 失败：任务错误中包含具体节点和 Agent 返回原因；不再出现 SSH key、host key 或 rsync 退出码问题。
- 后端重启：恢复 `pulling`、`packing`、`sending`、`syncing`、`loading` 状态的监控；已完成分片和归档继续复用。

代理设置只作用于控制平面的 registry 请求，不影响控制平面到节点或节点间高速直传。
