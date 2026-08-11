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

        不启用 PRAGMA foreign_keys=ON：任务允许在删除配方或强制删除集群后保留运行
        快照，传输记录也允许在删除 head 节点后留作审计，这些关系由应用层显式维护。
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
