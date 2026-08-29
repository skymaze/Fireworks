"""启动迁移幂等性：image_transfers 增加 registry_digest 列（真实版本展示 + tag 漂移检测）。

覆盖：
- 旧库（无该列）补列成功、既有行 registry_digest 为 NULL；
- 已存在该列时直接跳过（幂等，重复执行安全）。
"""

import sqlite3

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db_migrate import ensure_image_registry_digest_column

_OLD_TABLE_SQL = """
CREATE TABLE image_transfers (
    id INTEGER PRIMARY KEY,
    image VARCHAR(500),
    digest VARCHAR(128),
    head_node_id INTEGER,
    status VARCHAR(32),
    sync_jobs JSON,
    downloaded_bytes INTEGER DEFAULT 0,
    sent_bytes INTEGER DEFAULT 0,
    size_bytes INTEGER,
    error TEXT,
    created_at DATETIME,
    updated_at DATETIME
)
"""


def _cols(conn) -> list[str]:
    return [r[1] for r in conn.execute(
        text("PRAGMA table_info(image_transfers)"))]


def test_migration_adds_registry_digest_column_on_old_db(tmp_path):
    """旧库迁移：补列成功、旧行 registry_digest 为 NULL、重复执行幂等。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(_OLD_TABLE_SQL)
    conn.execute("INSERT INTO image_transfers (id, image, digest, status) "
                 "VALUES (1, 'example/app:1', 'sha256:aa', 'completed')")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    S = sessionmaker(bind=engine)
    db = S()
    assert "registry_digest" not in _cols(db.connection())

    ensure_image_registry_digest_column(db)  # 补列
    assert "registry_digest" in _cols(db.connection())

    ensure_image_registry_digest_column(db)  # 已存在 -> 幂等跳过
    assert "registry_digest" in _cols(db.connection())

    row = db.execute(
        text("SELECT registry_digest FROM image_transfers WHERE id=1")).first()
    assert row[0] is None  # 旧行补列为 NULL（版本未知，下次分发自动校准）
    db.close()


def test_migration_is_noop_when_column_already_exists(tmp_path):
    """新库（模型已含该列）：迁移直接跳过、数据不受影响。"""
    path = tmp_path / "fresh.db"
    conn = sqlite3.connect(path)
    conn.execute(_OLD_TABLE_SQL)
    conn.execute("ALTER TABLE image_transfers "
                 "ADD COLUMN registry_digest VARCHAR(128)")
    conn.execute("INSERT INTO image_transfers (id, image, digest, status, "
                 "registry_digest) VALUES (1, 'example/app:1', 'sha256:aa', "
                 "'completed', 'sha256:bb')")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    S = sessionmaker(bind=engine)
    db = S()
    ensure_image_registry_digest_column(db)  # 不应报错、不应改写
    row = db.execute(
        text("SELECT registry_digest FROM image_transfers WHERE id=1")).first()
    assert row[0] == "sha256:bb"
    db.close()
