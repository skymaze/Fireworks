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

from . import config
from . import background_tasks
from .db import Base, SessionLocal, engine
from .routers import (
    auth,
    clusters,
    images,
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
from .services import agent_ws, image_manager, llm_probe, metrics as metrics_svc
from .services import model_manager, task_monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)


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
    except Exception as e:  # noqa: BLE001 - 本地开发无 /data 时回退控制台
        logging.getLogger(__name__).warning("审计日志不可写（回退控制台）: %s", e)


def _migrate_sqlite():
    """轻量迁移：SQLite 无迁移框架，为已存在的表补列（列已存在则跳过）。"""
    import sqlalchemy as sa

    add_cols = {
        "clusters": [
            ("network_cidr", "VARCHAR(64)"),
            ("network_mtu", "INTEGER"),
            ("network_plan", "JSON"),
        ],
        "nodes": [
            ("cluster_id", "INTEGER"),
            ("agent_token", "VARCHAR(128)"),
        ],
        "recipes": [
            ("node_count", "INTEGER"),
            ("tensor_parallel", "INTEGER"),
        ],
    }
    try:
        with engine.begin() as conn:
            # 反射与 DDL 使用同一连接/事务。若 Inspector 绑定 engine，在 SQLite
            # 单连接池（测试/内存库）中反射结束时的 ROLLBACK 会撤销正在执行的迁移。
            insp = sa.inspect(conn)
            for table, cols in add_cols.items():
                existing = {c["name"] for c in insp.get_columns(table)}
                for name, ctype in cols:
                    if name not in existing:
                        conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {name} {ctype}"))
                        logger.info("迁移：%s 表新增列 %s", table, name)
            # nodes.cluster_id 回填：从 cluster_nodes 成员关系推导
            if "nodes" in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns("nodes")}
                if "cluster_id" in cols:
                    conn.execute(
                        sa.text(
                            "UPDATE nodes SET cluster_id = "
                            "(SELECT cn.cluster_id FROM cluster_nodes cn WHERE cn.node_id = nodes.id "
                            " LIMIT 1) WHERE cluster_id IS NULL "
                            "AND EXISTS (SELECT 1 FROM cluster_nodes cn2 WHERE cn2.node_id = nodes.id)"
                        )
                    )
            # 一个节点只能加入一个集群：清理历史重复成员并建唯一索引
            if "cluster_nodes" in insp.get_table_names():
                idxs = {ix["name"] for ix in insp.get_indexes("cluster_nodes")}
                if "uq_cluster_nodes_node" not in idxs:
                    conn.execute(
                        sa.text(
                            "DELETE FROM cluster_nodes WHERE id NOT IN "
                            "(SELECT MIN(id) FROM cluster_nodes GROUP BY node_id)"
                        )
                    )
                    conn.execute(
                        sa.text("CREATE UNIQUE INDEX uq_cluster_nodes_node ON cluster_nodes (node_id)")
                    )
                    logger.info("迁移：cluster_nodes 建 node_id 唯一索引（一节点一集群）")
            # 集群高速网段唯一：防止并发建集群时两个集群抢到同一 CIDR
            if "clusters" in insp.get_table_names():
                idxs = {ix["name"] for ix in insp.get_indexes("clusters")}
                if "uq_clusters_network_cidr" not in idxs:
                    dup = conn.execute(
                        sa.text(
                            "SELECT network_cidr FROM clusters "
                            "WHERE network_cidr IS NOT NULL AND network_cidr != '' "
                            "GROUP BY network_cidr HAVING COUNT(*) > 1 LIMIT 1"
                        )
                    ).fetchone()
                    if dup is None:
                        conn.execute(
                            sa.text(
                                "CREATE UNIQUE INDEX uq_clusters_network_cidr "
                                "ON clusters (network_cidr) "
                                "WHERE network_cidr IS NOT NULL AND network_cidr != ''"
                            )
                        )
                        logger.info("迁移：clusters 建 network_cidr 唯一索引")
                    else:
                        logger.warning(
                            "迁移跳过：clusters 存在重复网段 %s，需人工处理后重启", dup[0]
                        )
            # 总览按全局时间窗口扫描推理样本，单独的 ts 索引避免数据量增长后全表扫描。
            if "inference_samples" in insp.get_table_names():
                idxs = {ix["name"] for ix in insp.get_indexes("inference_samples")}
                if "ix_inference_ts" not in idxs:
                    conn.execute(
                        sa.text("CREATE INDEX ix_inference_ts ON inference_samples (ts)")
                    )
                    logger.info("迁移：inference_samples 建 ts 时间索引")
            # 旧版本依赖未启用的 SQLite FK CASCADE，删除任务后会留下孤儿节点、
            # 推理样本和压测结果。启动时清理一次，防止总览污染或主键复用串数据。
            tables = set(insp.get_table_names())
            if "tasks" in tables:
                # 在清理孤儿记录前，用历史上出现过的最大任务 ID 播种只增账本。
                # 即使旧 tasks 表没有 AUTOINCREMENT，后续发布也不会复用旧 ID。
                task_id_sources = ["SELECT id AS task_id FROM tasks"]
                for child in ("task_nodes", "inference_samples", "task_benchmarks"):
                    if child in tables:
                        task_id_sources.append(f"SELECT task_id FROM {child}")
                if "task_identities" in tables:
                    task_id_sources.append("SELECT id AS task_id FROM task_identities")
                    max_task_id = conn.execute(sa.text(
                        "SELECT MAX(task_id) FROM (" + " UNION ALL ".join(task_id_sources) + ")"
                    )).scalar()
                    if max_task_id:
                        conn.execute(sa.text(
                            "INSERT OR IGNORE INTO task_identities (id) VALUES (:id)"
                        ), {"id": max_task_id})
                for child in ("task_nodes", "inference_samples", "task_benchmarks"):
                    if child in tables:
                        result = conn.execute(sa.text(
                            f"DELETE FROM {child} WHERE NOT EXISTS "
                            f"(SELECT 1 FROM tasks WHERE tasks.id = {child}.task_id)"
                        ))
                        if result.rowcount:
                            logger.info("迁移：清理 %s 条 %s 孤儿记录", result.rowcount, child)
    except Exception as e:  # noqa: BLE001 - 迁移失败不阻断启动（首次建表时列已存在）
        logger.warning("SQLite 迁移跳过（表尚不存在或迁移失败）: %s", e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _setup_audit_logging()
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()
    with SessionLocal() as db:
        seed_recipe_sources(db)
    poller = background_tasks.spawn(metrics_svc.metrics_loop())
    # LLM 推理探针：running 任务实时 tok/s/TTFT（依赖 agent_ws 连接态判断 head 在线）
    probe_task = background_tasks.spawn(llm_probe.probe_task_loop())
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
    probe_task.cancel()
    # 统一关停后台任务：取消传输监控/健康检查/连接同步等，等其结束再关连接
    background_tasks.cancel_all()
    await asyncio.gather(poller, task_mon, probe_task, return_exceptions=True)
    await background_tasks.wait_all()
    await agent_ws.stop()
    from .services import agent_client
    await agent_client.close()


app = FastAPI(
    title="Fireworks - DGX Spark 集群管理工具",
    version="0.1.0",
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
    except Exception as e:  # noqa: BLE001
        logger.error("健康检查失败：SQLite 不可用 - %s", e)
        raise HTTPException(status_code=503, detail=f"数据库不可用: {e}")
    return {"status": "ok"}
