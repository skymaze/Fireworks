from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_connect(dbapi_conn, _record):
        """SQLite 并发加固：WAL 允许读写并发，busy_timeout 缓解 database is locked。

        注意：不启用 PRAGMA foreign_keys=ON。models.py 中多个外键是软引用且无 CASCADE
        （tasks.cluster_id / tasks.recipe_id 为 NOT NULL，model_downloads /
        image_transfers.head_node_id），启用后 delete_recipe / force 删集群 / 删
        传输 head 节点会变成 IntegrityError -> 500（SQLite 无法在线改 NOT NULL，
        需整表重建才可支持 SET NULL）。保持现状由应用层管理引用一致性。
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        # synchronous=NORMAL：WAL 下崩溃最多丢最近事务而不损坏，显著降低写延迟/锁窗
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
