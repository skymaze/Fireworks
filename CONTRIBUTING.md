# 贡献指南

感谢你对 Fireworks 的兴趣！贡献之前，请先阅读本指南与 [README](README.md)。

## 开发环境

后端（Python 3.11+，FastAPI）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests
```

前端（Node 24+，Nuxt 4）：

```bash
cd frontend && npm install && npm run build   # 构建校验
npm run dev                                    # 本地开发（可在 :3001 跑，避免占用控制平面端口）
```

本地更完整的联调：

```bash
docker compose up -d --build   # 控制平面（后端 :8000 + 前端 :3000）
```

## 提交流程

1. 从 `main` 切分支：`git checkout -b feat/xxx`（或 `fix/xxx`）。
2. 小步提交，一个提交只做一件事。
3. 提交信息遵守仓库既有风格（中文，主题行 + 简短正文）。
4. **提交前**：后端 `pytest backend/tests` 全部通过；前端 `npm run build` 通过；涉及 Agent 的改动顺手跑 `python -m py_compile agent/main.py`。
5. 开 PR：说明动机、改动要点、验证方式（能附截图/日志更佳）。

## 代码约定

- **注释与文档用中文**，澄清"为什么"而非复述代码。
- **不引入额外运行时依赖**：改动优先用标准库；确需新增第三方依赖时先在 issue 讨论并锁定精确版本。
- Agent 保持**单文件**（`agent/main.py`），除非确有拆分必要。
- 执行外部命令一律用 **argv 列表**，禁止 `shell=True`/`os.system`。
- 端到端涉及节点的高风险改动，请在真实 DGX 环境验证后在 PR 描述中说明。

## 测试

- 后端单测在 `backend/tests/`；新增逻辑尽量补回归用例（尤其鉴权、回拉安全、网络规划这类易回归点）。
- 前端本轮以构建校验为准。
- 当前无 CI「通过门槛」，但 PR 中被维护者抽查，未过测试的改动不会被合并。

## 议题（Issue）

- 提 bug 请附：版本（commit hash）、复现步骤、期望/实际结果、相关日志。
- 提功能请说明场景与预期行为。
- 安全相关请走 [SECURITY.md](SECURITY.md) 的私有披露，勿公开漏洞细节。
