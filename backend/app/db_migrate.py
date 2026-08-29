"""启动时幂等数据库迁移（SQLite）：历史库结构修正，集中于此，无迁移框架依赖。

新增迁移时保持幂等：重复执行安全、不阻塞启动（失败恢复原状并抛出，
由入口决定是否继续）。

实现说明：重建表全部使用显式 SQL（同一 Connection 顺序执行），不依赖
SQLAlchemy create_all 的 checkfirst——create_all 在 Session 事务与 SQLite
DDL 隐式提交交互时会误判表已存在而跳过建表（表现为迁移后数据表为空）。
"""

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import engine

logger = logging.getLogger(__name__)

# 主键必须单调不复用的表（因存在允许悬空保留的外部引用，见各模型注释）：
# SQLite 默认 INTEGER PRIMARY KEY 会复用已删除的最大 ROWID，导致遗留引用
# 串到后创建的记录（删除集群后新建集群出现上个集群数据即由此而来）。
_AUTOINCREMENT_TABLES = ("clusters", "nodes", "recipes")


def _table_sql(conn, name: str) -> str | None:
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": name},
    ).first()
    return row[0] if row else None


def _table_exists(conn, name: str) -> bool:
    return (
        conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": name},
        ).first()
        is not None
    )


def _explicit_indexes(conn, table: str) -> list[tuple[str, str]]:
    """表上的显式索引 (name, 完整 CREATE 语句)。

    SQLite 的 ALTER TABLE RENAME 会保留表上的显式索引且索引名不变，重建表后
    需删除旧索引并重建同名索引。表级 UNIQUE 约束的自动索引（sql IS NULL，
    名随表名变化）随表迁移，无需处理。
    """
    rows = conn.execute(
        text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name=:t AND sql IS NOT NULL"
        ),
        {"t": table},
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _autoincrement_ddl(old_sql: str) -> str:
    """从旧表 CREATE 语句派生 AUTOINCREMENT 版本。

    旧表 id 为普通列 + 表级 PRIMARY KEY (id)。改为列级
    `id ... PRIMARY KEY AUTOINCREMENT` 并删除表级主键约束。
    不依赖行结构（兼容 SQLAlchemy 生成的多行格式与手写单行格式）。
    """
    new = re.sub(
        r'\b"?id"?\s+INTEGER\s+NOT\s+NULL',
        "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT",
        old_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    new = re.sub(
        r',?\s*PRIMARY\s+KEY\s*\(\s*"?id"?\s*\)',
        "",
        new,
        flags=re.IGNORECASE,
    )
    return new


def _restore_legacy(conn, table: str, legacy: str) -> None:
    """上次迁移中断的残留自愈：丢弃不完整新表，数据表恢复原名。

    中断场景下数据仍完整保留在 {table}_legacy，新表为空或半迁移状态。
    恢复后表无 AUTOINCREMENT，由调用方重新执行迁移路径把数据搬回。
    """
    conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    conn.execute(text(f"ALTER TABLE {legacy} RENAME TO {table}"))
    logger.info("迁移自愈：已恢复中断的 %s 数据表，重新执行迁移", table)


def ensure_table_autoincrement(db: Session, table: str) -> None:
    """迁移单表：id 启用 AUTOINCREMENT（主键单调不复用）。

    重建表（ALTER TABLE 无法为既有表添加 AUTOINCREMENT）：删除旧表显式索引 ->
    改名让位 -> 显式 CREATE 新表（列级 AUTOINCREMENT 主键）-> 数据原样搬回
    （id 显式保留，sqlite_sequence 随之更新）-> 重建索引 -> 丢弃旧表。
    幂等：已启用或表不存在时直接跳过；上次中断的 *_legacy 残留自动恢复后
    重跑，不会丢数据。所有 SQL 统一在同一 Connection 上执行。
    """
    if table not in _AUTOINCREMENT_TABLES:
        raise ValueError(f"未登记的主键单调性迁移表: {table}")
    conn = db.connection()
    if _table_exists(conn, f"{table}_legacy"):
        # 上次迁移中断残留：恢复后重走本函数（此时表无 AUTOINCREMENT）
        _restore_legacy(conn, table, f"{table}_legacy")
        db.commit()
        return ensure_table_autoincrement(db, table)
    sql = _table_sql(conn, table)
    if not sql or "AUTOINCREMENT" in sql.upper():
        return
    legacy = f"{table}_legacy"
    indexes = _explicit_indexes(conn, table)
    try:
        # 显式索引名不随表改名：先删，重建表后再以原语句重建（表名此时指新表）
        for name, _sql in indexes:
            conn.execute(text(f'DROP INDEX "{name}"'))
        conn.execute(text(f"ALTER TABLE {table} RENAME TO {legacy}"))
        conn.execute(text(_autoincrement_ddl(sql)))
        # 只搬两个版本都存在的列：旧库可能比当前模型多列（旧版本字段、
        # 未进 git 的实验列等），新表以当前模型为准，多出的列不搬
        old_cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({legacy})"))]
        new_cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))]
        common = [c for c in old_cols if c in new_cols]
        col_sql = ", ".join(common)
        conn.execute(
            text(f"INSERT INTO {table} ({col_sql}) SELECT {col_sql} FROM {legacy}")
        )
        for _name, index_sql in indexes:
            conn.execute(text(index_sql))
        conn.execute(text(f"DROP TABLE {legacy}"))
        db.commit()
        logger.info(
            "迁移完成：%s.id 已启用 AUTOINCREMENT（防止删除后 ID 复用串数据）", table
        )
    except Exception:
        # DDL 在 SQLite 隐式提交，异常后半迁移状态需尽力恢复（rollback 后
        # 原连接失效，用 db.connection() 重新获取），避免残留 legacy 表
        db.rollback()
        try:
            conn2 = db.connection()
            if _table_exists(conn2, legacy):
                conn2.execute(text(f"DROP TABLE IF EXISTS {table}"))
                conn2.execute(text(f"ALTER TABLE {legacy} RENAME TO {table}"))
                db.commit()
        except Exception:
            logger.exception("%s 表迁移恢复失败，请检查数据库后手动处理", table)
        raise


def _table_cols(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))]


def ensure_task_health_column(db: Session) -> None:
    """幂等迁移：为 tasks 添加 health 列（任务层面 compose 健康检查状态）。"""
    conn = db.connection()
    cols = _table_cols(conn, "tasks")
    if "health" in cols:
        return
    conn.execute(text(
        "ALTER TABLE tasks ADD COLUMN health VARCHAR(16) NOT NULL DEFAULT ''"
    ))
    db.commit()
    logger.info("迁移完成：tasks.health 已添加（任务层面健康状态）")


def drop_tasknode_health_column(db: Session) -> None:
    """幂等迁移：删除 task_nodes.container_health（健康移归任务层面）。

    节点层面只保留容器状态；v0.5 曾引入的节点健康列不再使用。
    SQLite >= 3.35 支持 DROP COLUMN（Python 3.11+ 内置 sqlite 满足）。
    """
    conn = db.connection()
    if "container_health" not in _table_cols(conn, "task_nodes"):
        return
    try:
        conn.execute(text("ALTER TABLE task_nodes DROP COLUMN container_health"))
        db.commit()
        logger.info("迁移完成：task_nodes.container_health 已删除（健康移至任务层面）")
    except Exception:
        # 极端 sqlite 版本不支持 DROP COLUMN 时仅告警，列保留但不使用
        db.rollback()
        logger.warning("task_nodes.container_health 删除失败（保留但不再使用）")


def ensure_image_registry_digest_column(db: Session) -> None:
    """幂等迁移：镜像传输任务记录 registry 内容 digest（真实版本展示 + tag 漂移检测）。

    既有行 registry_digest 为 NULL：控制平面归档若由旧版产生、sidecar 无记录，
    下次创建传输任务时会被视为「未知版本」，自动重拉一次并补齐记录。
    """
    conn = db.connection()
    if "registry_digest" in _table_cols(conn, "image_transfers"):
        return
    conn.execute(text(
        "ALTER TABLE image_transfers ADD COLUMN registry_digest VARCHAR(128)"
    ))
    db.commit()
    logger.info("迁移完成：image_transfers.registry_digest 已添加（registry 内容 digest）")


def run_startup_migrations(db: Session) -> None:
    """启动时执行全部幂等迁移（按登记顺序）。"""
    if not engine.url.drivername.startswith("sqlite"):
        return
    for table in _AUTOINCREMENT_TABLES:
        ensure_table_autoincrement(db, table)
    ensure_task_health_column(db)
    drop_tasknode_health_column(db)
    ensure_image_registry_digest_column(db)
