"""删除集群的回归：主键单调不复用 + 关联数据一并清理。

SQLite 默认 INTEGER PRIMARY KEY 复用已删除的最大 ROWID：删除集群后，
遗留引用（历史任务等）会串到复用同一 ID 的新集群，表现为「新建集群出现
上个集群的数据」。相关表启用 AUTOINCREMENT（与 tasks 表一致），删除集群时
显式清理关联任务及历史数据，并对升级自旧库的部署提供启动幂等迁移。
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.db_migrate import ensure_table_autoincrement
from app.models import Cluster


def test_cluster_id_not_reused_after_delete():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    c1 = Cluster(name="c1", network_type="roce", network_cidr="10.0.0.0/16")
    db.add(c1)
    db.commit()
    old_id = c1.id

    db.delete(c1)
    db.commit()

    # 新集群绝不能复用被删除集群的 id（否则悬空的历史任务引用会串到新集群）
    c2 = Cluster(name="c2", network_type="roce", network_cidr="10.1.0.0/16")
    db.add(c2)
    db.commit()
    assert c2.id > old_id
    db.close()


def _create_legacy_clusters_table(db) -> None:
    """模拟真实旧库：id 无 AUTOINCREMENT、表级主键、带显式唯一索引。

    显式索引（uq_clusters_network_cidr）是迁移的核心陷阱：SQLite 的
    ALTER TABLE RENAME 保留索引名，重建表时必须先删后建，否则 create_all
    重建同名索引报 already exists。
    """
    db.execute(text(
        "CREATE TABLE clusters ("
        "  id INTEGER NOT NULL,"
        "  name VARCHAR(100) NOT NULL,"
        "  description TEXT,"
        "  network_type VARCHAR(32) NOT NULL DEFAULT 'roce',"
        "  network_cidr VARCHAR(64),"
        "  network_mtu INTEGER,"
        "  network_plan JSON,"
        "  created_at DATETIME NOT NULL,"
        "  PRIMARY KEY (id),"
        "  UNIQUE (name)"
        ")"
    )).close()
    db.execute(text(
        "CREATE UNIQUE INDEX uq_clusters_network_cidr ON clusters (network_cidr)"
        " WHERE network_cidr IS NOT NULL AND network_cidr != ''"
    )).close()
    db.execute(text(
        "INSERT INTO clusters (id, name, network_type, created_at)"
        " VALUES (1, 'legacy', 'roce', '2026-01-01 00:00:00')"
    )).close()
    db.commit()


def test_migrate_legacy_cluster_table_enables_autoincrement():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    # 换掉刚建的新表，模拟旧库结构
    db.execute(text("DROP TABLE clusters")).close()
    db.commit()
    _create_legacy_clusters_table(db)

    ensure_table_autoincrement(db, "clusters")

    # 表已重建为 AUTOINCREMENT，数据与显式索引保留，无 legacy 残留
    row = db.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='clusters'")
    ).first()
    assert "AUTOINCREMENT" in row[0].upper()
    assert db.query(Cluster).filter_by(name="legacy").one().id == 1
    idx = db.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='clusters'"
    )).fetchall()
    assert "uq_clusters_network_cidr" in {r[0] for r in idx}
    assert db.execute(text(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%_legacy'"
    )).scalar() == 0

    # 迁移后删除最大 id 不再复用
    db.execute(text("DELETE FROM clusters WHERE id = 1"))
    db.commit()
    c2 = Cluster(name="fresh", network_type="roce")
    db.add(c2)
    db.commit()
    assert c2.id > 1
    db.close()


def test_migrate_heals_interrupted_legacy_residue():
    """上次迁移中断（legacy 残留 + 空 AUTOINCREMENT 新表）自愈并恢复数据。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.execute(text("DROP TABLE clusters")).close()
    db.commit()
    _create_legacy_clusters_table(db)
    # 模拟中断状态：旧表已改名、新空表已建（AUTOINCREMENT）
    db.execute(text("ALTER TABLE clusters RENAME TO clusters_legacy")).close()
    db.execute(text(
        "CREATE TABLE clusters ("
        "  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
        "  name VARCHAR(100) NOT NULL,"
        "  description TEXT,"
        "  network_type VARCHAR(32) NOT NULL DEFAULT 'roce',"
        "  network_cidr VARCHAR(64),"
        "  network_mtu INTEGER,"
        "  network_plan JSON,"
        "  created_at DATETIME NOT NULL,"
        "  UNIQUE (name)"
        ")"
    )).close()
    db.commit()

    ensure_table_autoincrement(db, "clusters")

    # 数据从 legacy 搬回，表为 AUTOINCREMENT，legacy 清理干净
    assert db.query(Cluster).filter_by(name="legacy").one().id == 1
    assert db.execute(text(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%_legacy'"
    )).scalar() == 0
    row = db.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='clusters'")
    ).first()
    assert "AUTOINCREMENT" in row[0].upper()
    db.close()


def test_migrate_is_idempotent():
    """已启用 AUTOINCREMENT 的库重复执行迁移无副作用。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Cluster(name="c1", network_type="roce"))
    db.commit()
    before = db.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='clusters'")
    ).first()[0]

    ensure_table_autoincrement(db, "clusters")

    after = db.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='clusters'")
    ).first()[0]
    assert before == after
    assert db.query(Cluster).count() == 1
    db.close()


# ---------- 删除集群清理关联任务及历史数据 ----------


def test_delete_cluster_removes_tasks_and_history(monkeypatch):
    """删除集群时其下已结束任务、推理统计与压测记录一并清理。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.models import (
        Cluster,
        ClusterNode,
        InferenceSample,
        Node,
        Task,
        TaskBenchmark,
        TaskNode,
    )
    from app.routers import clusters

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    node = Node(id=1, name="n1", ip="192.0.2.1")
    cluster = Cluster(id=1, name="c1", network_type="roce")
    task = Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="stopped")
    db.add_all([node, cluster, task])
    db.commit()
    db.add(ClusterNode(cluster_id=1, node_id=1, net_index=1))
    db.add(TaskNode(task_id=1, node_id=1, role="head", node_rank=0))
    db.add(InferenceSample(task_id=1, node_id=1, ts=0.0, data={}))
    db.add(TaskBenchmark(task_id=1, ts=0.0, result={}))
    db.commit()
    # 容器已停止（正常流程），compose_down 仅尽力而为（async，与真实实现一致）
    async def fake_compose_down(_node, _name):
        return {}

    monkeypatch.setattr(clusters.agent_client, "compose_down", fake_compose_down)
    monkeypatch.setattr(clusters.agent_ws, "broadcast", lambda *_a, **_k: None)

    result = clusters.delete_cluster(1, cleanup_network=False, db=db)

    assert result["ok"] and result["deleted_tasks"] == 1
    assert result["warnings"] == []
    assert db.query(Task).count() == 0
    assert db.query(TaskNode).count() == 0
    assert db.query(InferenceSample).count() == 0
    assert db.query(TaskBenchmark).count() == 0
    assert db.query(ClusterNode).count() == 0
    assert db.query(Cluster).count() == 0
    assert db.get(Node, 1).cluster_id is None  # 成员占用释放
    db.close()


def test_delete_cluster_refuses_active_tasks():
    """运行中/已发布/暂停任务阻止删除集群（容器仍在节点上）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from fastapi import HTTPException

    from app.db import Base
    from app.models import Cluster, Node, Task
    from app.routers import clusters

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add_all([
        Node(id=1, name="n1", ip="192.0.2.1"),
        Cluster(id=1, name="c1", network_type="roce"),
        Task(id=1, name="t1", recipe_id=1, cluster_id=1, status="running"),
    ])
    db.commit()

    with pytest.raises(HTTPException) as exc:
        clusters.delete_cluster(1, cleanup_network=False, db=db)
    assert exc.value.status_code == 409
    # 集群与任务都未被删除
    assert db.query(Cluster).count() == 1
    assert db.query(Task).count() == 1
    db.close()


def test_migrate_keeps_columns_common_to_both_versions():
    """旧表列多于当前模型（旧版本字段）时只搬交集列，迁移不失败。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.execute(text("DROP TABLE recipes")).close()
    db.commit()
    # 模拟带旧版本字段的 recipes（origin_source_id 等不在当前模型）
    db.execute(text(
        "CREATE TABLE recipes ("
        "  id INTEGER NOT NULL,"
        "  name VARCHAR(200) NOT NULL,"
        "  description TEXT,"
        "  image VARCHAR(500),"
        "  compose_template TEXT NOT NULL,"
        "  variables JSON NOT NULL,"
        "  is_seed BOOLEAN NOT NULL,"
        "  origin_source_id INTEGER,"
        "  origin_path VARCHAR(500),"
        "  installed_version VARCHAR(64),"
        "  installed_at DATETIME,"
        "  node_count INTEGER,"
        "  tensor_parallel INTEGER,"
        "  created_at DATETIME NOT NULL,"
        "  updated_at DATETIME NOT NULL,"
        "  name_en VARCHAR(200),"
        "  description_en TEXT,"
        "  PRIMARY KEY (id),"
        "  UNIQUE (name)"
        ")"
    )).close()
    db.execute(text(
        "INSERT INTO recipes (id, name, compose_template, variables, is_seed, created_at, updated_at, origin_source_id)"
        " VALUES (1, 'legacy-recipe', '{}', '[]', 0, '2026-01-01', '2026-01-01', 7)"
    )).close()
    db.commit()

    ensure_table_autoincrement(db, "recipes")

    from app.models import Recipe
    row = db.query(Recipe).filter_by(name="legacy-recipe").one()
    assert row.id == 1  # 数据保留（交集列）
    sql = db.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='recipes'"
    )).first()[0]
    assert "AUTOINCREMENT" in sql.upper()
    # 新表由旧表 DDL 派生，旧版本字段保留、数据不丢
    cols = [r[1] for r in db.execute(text("PRAGMA table_info(recipes)")).fetchall()]
    assert "origin_source_id" in cols
    assert db.execute(text("SELECT origin_source_id FROM recipes WHERE id = 1")).scalar() == 7
    assert db.execute(text(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%_legacy'"
    )).scalar() == 0
    db.close()
