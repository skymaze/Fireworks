"""任务运行时写入协调：在 SQLite 未启用外键时避免删除竞态产生孤儿数据。"""

from collections.abc import Collection

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import Task


def lock_task_for_write(
    db: Session,
    task_id: int,
    statuses: Collection[str] | None = None,
) -> Task | None:
    """结束旧读事务并锁定任务，供运行时数据写入或聚合根删除。

    SQLite 使用 ``BEGIN IMMEDIATE`` 串行化写事务；其它数据库使用行锁。调用者须在
    同一事务中完成子记录写入/删除并 commit，才能保证删除不会与晚到写入交错。
    """
    db.rollback()
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))

    query = db.query(Task).filter(Task.id == task_id)
    if statuses is not None:
        query = query.filter(Task.status.in_(tuple(statuses)))
    if dialect != "sqlite":
        query = query.with_for_update()
    return query.populate_existing().first()
