"""Fireworks 控制平面入口。"""

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import background_tasks, config
from . import db_migrate
from .db import Base, SessionLocal, engine
from .routers import (
    auth,
    clusters,
    images,
    inference,
    internal,
    models,
    nodes,
    overview,
    recipes,
    tasks,
    ws,
)
from .security import get_current_user
from .seed import seed_recipe_sources
from .services import agent_ws, image_manager, llm_stats, model_manager, task_monitor
from .services import metrics as metrics_svc
from .services import recipe_source as recipe_source_svc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)

APP_VERSION = config.APP_VERSION  # 与 config 同源（升级提醒的期望代理版本基准）


# ---------- 可观测性：请求 ID + 访问日志 ----------


class RequestLogMiddleware:
    """为每个 HTTP 请求生成/透传 X-Request-ID，并按行记录访问日志。

    轻量 ASGI 中间件：出站响应带请求 ID，便于与前端/Nitro 日志串查；
    访问日志记录 method/path/状态/耗时。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Agent 回拉文件流 = 逐 1MB 分块 GET：跳过访问日志，避免大模型/镜像分发刷屏
        # （每 166GB 模型分发会产生十数万行 INFO；如排查可用 DEBUG 或看后端访问日志）
        if scope.get("path", "").startswith(
            ("/api/models/files/", "/api/images/archive/")
        ):
            await self.app(scope, receive, send)
            return
        req_id = uuid.uuid4().hex[:12]
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-request-id" and value:
                req_id = value.decode("latin-1", "replace")
                break
        status = {"code": 0}
        start = time.monotonic()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", req_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = (time.monotonic() - start) * 1000
            logger.info(
                "%s %s -> %s (%.0fms) [%s]",
                scope.get("method", ""), scope.get("path", ""),
                status["code"], elapsed, req_id,
            )


# ---------- 审计日志落盘 ----------

AUDIT_LOG_FILE = os.environ.get("AUDIT_LOG_PATH", "/data/audit.log")


def _setup_audit_logging() -> None:
    """把 `fireworks.audit`（关键操作审计）落盘到固定文件，防止仅存在于容器日志。"""
    audit_logger = logging.getLogger("fireworks.audit")
    # 已挂过文件 handler 则跳过（测试进程内多次 lifespan、重复启动等场景防重复落盘）
    if any(isinstance(h, logging.FileHandler) for h in audit_logger.handlers):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        Path(AUDIT_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(AUDIT_LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        audit_logger.addHandler(fh)
    except Exception as e:
        logging.getLogger(__name__).warning("审计日志不可写（回退控制台）: %s", e)


def _migrate_node_optimize_column() -> None:
    """升级自旧库的部署：为 nodes 表补建 optimize_result 列（幂等）。

    create_all 只建新表、不补列；旧库升级到本版本时缺失该列，手动 ALTER 补齐。
    初始优化为可选功能，缺列会导致「添加即优化/手动优化」落库时报错。
    """
    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(engine).get_columns("nodes")}
    if "optimize_result" in cols:
        return
    ddl_type = {"sqlite": "JSON", "postgresql": "JSON", "mysql": "JSON"}.get(
        engine.dialect.name, "JSON"
    )
    with SessionLocal() as db:
        db.execute(
            text(f"ALTER TABLE nodes ADD COLUMN optimize_result {ddl_type}")
        )
        db.commit()
    logger.info("已为旧库补建 nodes.optimize_result 列（初始优化状态）")


@asynccontextmanager
async def lifespan(_: FastAPI):
    _setup_audit_logging()
    Base.metadata.create_all(bind=engine)
    _migrate_node_optimize_column()
    with SessionLocal() as db:
        db_migrate.run_startup_migrations(db)
        recipe_source_svc.recover_interrupted_syncs(db)
        seed_recipe_sources(db)
        llm_stats.ensure_inference_indexes(db)
        # 升级清理：删除旧格式的推理统计样本（幂等，重启即清）
        llm_stats.cleanup_legacy_inference_samples(db)
    poller = background_tasks.spawn(metrics_svc.metrics_loop())
    # LLM 推理统计：running 任务实时 tok/s/TTFT（依赖 agent_ws 连接态判断 head 在线）
    stats_task = background_tasks.spawn(llm_stats.stats_task_loop())
    # 后端重启后，对存量 running/published 任务补发健康检查
    resumed = tasks.schedule_health_checks()
    if resumed:
        logging.getLogger(__name__).info("已补发 %d 个任务健康检查", resumed)
    # 恢复进行中的模型下载/同步监控
    resumed_dl = model_manager.resume_download_monitors()
    if resumed_dl:
        logging.getLogger(__name__).info("已恢复 %d 个模型下载任务", resumed_dl)
    resumed_img = image_manager.resume_image_monitors()
    if resumed_img:
        logging.getLogger(__name__).info("已恢复 %d 个镜像传输任务", resumed_img)
    # 运行中任务容器状态监控（容器退出 -> 任务 stopped）
    task_mon = background_tasks.spawn(task_monitor.task_monitor_loop())
    resumed_tasks = await task_monitor.resume_task_monitors()
    if resumed_tasks:
        logging.getLogger(__name__).info("已补查 %d 个运行中任务容器状态", resumed_tasks)
    # Agent WebSocket 实时通道（指标/容器事件/日志流/传输进度）
    await agent_ws.start()
    yield
    poller.cancel()
    task_mon.cancel()
    stats_task.cancel()
    # 统一关停后台任务：取消传输监控/健康检查/连接同步等，等其结束再关连接
    background_tasks.cancel_all()
    await asyncio.gather(poller, task_mon, stats_task, return_exceptions=True)
    await background_tasks.wait_all()
    await agent_ws.stop()
    from .services import agent_client
    await agent_client.close()


app = FastAPI(
    title="Fireworks - DGX Spark 集群管理工具",
    version=APP_VERSION,
    lifespan=lifespan,
)

# CORS：仅放行配置的来源（默认前端开发源 localhost:3000）。
# 同源部署（前端 /api 代理）下 CORS 不参与；allow_credentials=True 以支持会话 cookie。
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 访问日志/请求 ID（放 CORS 之后，内层先执行）
app.add_middleware(RequestLogMiddleware)

# 公开：认证端点 / /api/health
app.include_router(auth.router)
# 内部：Agent 回拉模型文件/镜像归档（端点内 get_user_or_agent 双门控）
app.include_router(internal.router)
# 业务端点：全部要求登录会话
app.include_router(overview.router, dependencies=[Depends(get_current_user)])
app.include_router(nodes.router, dependencies=[Depends(get_current_user)])
app.include_router(clusters.router, dependencies=[Depends(get_current_user)])
app.include_router(recipes.router, dependencies=[Depends(get_current_user)])
app.include_router(tasks.router, dependencies=[Depends(get_current_user)])
app.include_router(models.router, dependencies=[Depends(get_current_user)])
app.include_router(images.router, dependencies=[Depends(get_current_user)])
app.include_router(inference.router, dependencies=[Depends(get_current_user)])
# WS 在 ws_events 内手工校验会话 cookie（WebSocket 不走 HTTP 依赖注入）
app.include_router(ws.router)


@app.get("/api/health")
def health():
    """就绪探针：容器编排 readiness 用——除存活外校验 SQLite 读/写路径可用。"""
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
            # 临时表读写探测：事务结束即丢弃，不污染业务数据
            conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS _fw_health (v INTEGER)"))
            conn.execute(text("INSERT INTO _fw_health(v) VALUES (1)"))
            conn.execute(text("DELETE FROM _fw_health"))
    except Exception as e:
        logger.error("健康检查失败：SQLite 不可用 - %s", e)
        raise HTTPException(status_code=503, detail=f"数据库不可用: {e}")
    return {"status": "ok", "version": APP_VERSION}
