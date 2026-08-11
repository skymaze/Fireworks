# Fireworks 发布流程

## 1. 准备 release candidate

- 更新根目录 `VERSION`、后端与 Agent 的 `APP_VERSION`、前端 `package.json` / `package-lock.json`。
- 更新 `CHANGELOG.md` 和 `docs/releases/v<version>.md`。
- 确认 `main` 工作区干净，且计划发布的提交已经推送到 `origin/main`。
- 执行：

```bash
.venv/bin/pytest -q backend/tests
cd frontend && npm ci --no-audit --no-fund && npm run typecheck && npm run build
```

涉及 Agent 时额外执行：

```bash
.venv/bin/python -m py_compile agent/main.py
```

## 2. 镜像与数据检查

- 确认 GHCR 与阿里云 ACR 仓库允许公开拉取，GitHub Actions 具有 packages、contents、attestations 权限。
- 确认 backend 镜像可以为 Python 3.10–3.13、amd64/arm64 生成 Agent 离线 wheelhouse。
- 确认 GitHub Environment `aliyun-release` 已配置 ACR 地址、命名空间、登录名和登录密码。
- 使用目标版本数据库做一次启动和登录冒烟测试；已有正式版本升级前备份 `fireworks-db` 卷。

## 3. 创建稳定标签

release workflow 会校验标签、`VERSION`、后端/前端版本和发布说明完全一致。稳定版本使用带签名或 annotated tag：

```bash
git switch main
git pull --ff-only
git tag -a v0.1.0 -m "Fireworks v0.1.0"
git push origin v0.1.0
```

推送标签后，流水线会：

1. 构建 `linux/amd64` 与 `linux/arm64` backend/frontend 镜像。
2. 向 GHCR 与阿里云 ACR 同时推送 `0.1.0`、`0.1`、`0`、`latest` 和 commit SHA 标签。
3. 为镜像生成 provenance 与 registry attestation。
4. 使用 `docs/releases/v0.1.0.md` 创建 GitHub Release。

## 4. 发布后验证

```bash
FW_IMAGE_TAG=0.1.0 COOKIE_SECURE=0 docker compose -f docker-compose.prod.yml up -d --pull always
curl -fsS http://127.0.0.1:8000/api/health
```

健康检查应返回 `{"status":"ok","version":"0.1.0"}`。随后验证首次登录、节点信息、实时日志、配方源同步、集群预检和至少一个任务发布流程。

## 5. 回滚

- 控制平面镜像回滚：把 `FW_IMAGE_TAG` 改为上一个稳定版本并执行 `docker compose up -d`。
- 数据回滚：停止服务后恢复发布前的 `fireworks-db` 备份；不要让旧版本直接打开已经迁移且不兼容的数据库。
- Agent 回滚：使用对应旧版本控制平面的“重新部署 Agent”。
