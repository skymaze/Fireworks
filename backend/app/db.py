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


def release_db(db) -> None:
    """提前把连接归还连接池（幂等）：close 后会话仍可复用，下次查询重新取连接。

    端点/后台循环在 await 长 agent 网络 I/O 前调用：不可达节点的超时探测
    （连接 5s × 最多 3 次重试）可达数十秒，若带着 ORM 会话等待，少数并发轮询
    就会把 QueuePool（5+10）占满，全后端的 DB 操作排队 30s 后报 QueuePool
    timeout——表现为整站假死。注意 close 会把 ORM 实例分离（已加载的列属性
    仍可读），之后不要对分离实例做懒加载或经由原会话刷新它。
    """
    db.close()
