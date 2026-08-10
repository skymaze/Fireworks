"""配方源（FireworksRecipes git）集成测试：独立内存库 + 临时 git 源仓库。

覆盖：建源 / 同步（manifest 驱动） / 目录 / 安装（每次必新建行、永不覆盖） /
溯源列 / 缺 manifest 的源报错视为失败 / README 路径穿越防御 / 仅同步不写本地。
"""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.db import Base, get_db
from app.main import app
from app.models import Recipe, RecipeSource
from app.services import recipe_source

PASSWORD = "SuperSecret123"

RECIPE_JSON = {
    "name": "Test-Model（专属镜像）",
    "name_en": "Test-Model (dedicated image)",
    "description": "dummy",
    "description_en": "dummy en",
    "version": "1.2.3",
    "image": "test/img:1.2.3",
    "nodes": 2,             # 固定拓扑：确切节点数（配方级属性，发布时须恰好匹配）
    "tensor_parallel": 2,
    "compose_template": (
        "services:\n  test:\n    image: ${IMAGE:-test/img:1.2.3}\n"
        "    command:\n      - bash\n      - -lc\n      - echo ok\n"
    ),
    "variables": [
        {"key": "MASTER_ADDR", "label": "Head 节点", "label_en": "Head node", "type": "string",
         "source": "cluster", "auto": "head_roce_ip", "required": True},
        {"key": "MASTER_PORT", "label": "端口", "label_en": "Port", "type": "int",
         "source": "user", "default": "25000"},
        {"key": "NODES_TOTAL", "label": "节点数", "label_en": "Node count", "type": "int",
         "source": "cluster", "auto": "nodes_total", "required": True},
    ],
}

README_MD = "# Test-Model\n\n说明文档。\n"


def _make_repo(tmppath, with_manifest=True):
    """构造临时 git 源仓库并返回其路径。"""
    repo = tmppath / "src-repo"
    repo.mkdir()
    if with_manifest:
        (repo / "recipes").mkdir(parents=True)
        (repo / "recipes" / "index.json").write_text(json.dumps({
            "schema": 1,
            "recipes": [{
                "id": "test-model",
                "provider": "test-inc",
                "model": "test-inc/Test-Model",
                "path": "models/test-model/recipe/fireworks.recipe.json",
                "readme": "models/test-model/recipe/README.md",
                "version": "1.2.3",
                "dtype": "nvfp4",
                "context_length": 1048576,
                "nodes": 2,
                "tensor_parallel": 2,
                "description": "中文描述",
                "description_en": "English description",
                "readme": "models/test-model/recipe/README.md",
            }],
        }), encoding="utf-8")
    model = repo / "models" / "test-model" / "recipe"
    model.mkdir(parents=True)
    (model / "fireworks.recipe.json").write_text(json.dumps(RECIPE_JSON, ensure_ascii=False), encoding="utf-8")
    (model / "README.md").write_text(README_MD, encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    assert git("commit", "-m", "recipes").returncode == 0
    # 同时提供非默认分支，覆盖远端分支发现与切换。
    assert git("switch", "-c", "dev").returncode == 0
    (repo / "DEV").write_text("dev branch\n", encoding="utf-8")
    git("add", "DEV")
    assert git("commit", "-m", "dev").returncode == 0
    assert git("switch", "main").returncode == 0
    return str(repo)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RECIPE_SRC_DIR", str(tmp_path / "mirror"))
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)

    def _test_db():
        db = S()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _test_db
    client = TestClient(app)
    client.post("/api/auth/setup", json={"username": "admin", "password": PASSWORD})
    yield client, S
    app.dependency_overrides.clear()


def _add_source(client, url) -> int:
    r = client.post("/api/recipes/sources",
                    json={"name": "test-source", "url": url, "branch": "main"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_source_discovers_default_branch_updates_and_deletes_mirror(env, tmp_path):
    """添加无需填写分支；分支可切换；删除同时清理镜像但保留本地配方。"""
    client, S = env
    repo = _make_repo(tmp_path)

    discovered = client.post("/api/recipes/sources/discover", json={"url": repo})
    assert discovered.status_code == 200, discovered.text
    assert discovered.json() == {"default_branch": "main", "branches": ["dev", "main"]}

    created = client.post(
        "/api/recipes/sources",
        json={"name": "auto-source", "url": repo},
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    assert created.json()["branch"] == "main"

    not_synced = client.get(f"/api/recipes/sources/{source_id}/catalog")
    assert not_synced.status_code == 422
    assert not_synced.json()["detail"]["code"] == "catalog_not_synced"

    synced = client.post(f"/api/recipes/sources/{source_id}/sync")
    assert synced.status_code == 200, synced.text
    with S() as db:
        source = db.get(RecipeSource, source_id)
        mirror_dir = source.mirror_dir
        db.add(Recipe(name="independent", compose_template="services: {}", variables=[]))
        db.commit()
    mirror_path = tmp_path / "mirror" / mirror_dir
    assert mirror_path.is_dir()

    updated = client.patch(
        f"/api/recipes/sources/{source_id}", json={"branch": "dev"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["branch"] == "dev"
    assert updated.json()["status"] == "new"
    assert updated.json()["last_commit"] is None

    synced_dev = client.post(f"/api/recipes/sources/{source_id}/sync")
    assert synced_dev.status_code == 200, synced_dev.text
    expected_dev = subprocess.run(
        ["git", "rev-parse", "dev"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert synced_dev.json()["last_commit"] == expected_dev

    deleted = client.delete(f"/api/recipes/sources/{source_id}")
    assert deleted.status_code == 200, deleted.text
    assert not mirror_path.exists()
    with S() as db:
        assert db.get(RecipeSource, source_id) is None
        assert db.query(Recipe).filter_by(name="independent").count() == 1


def test_source_rejects_unknown_branch(env, tmp_path):
    client, _ = env
    repo = _make_repo(tmp_path)
    response = client.post(
        "/api/recipes/sources",
        json={"name": "bad-branch", "url": repo, "branch": "missing"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "recipe_source_branch_not_found"


def test_source_mirror_names_do_not_collide_and_delete_is_isolated(env, tmp_path):
    """不同源的名称即使产生相同 slug，也必须使用独立镜像目录。"""
    client, S = env
    repo = _make_repo(tmp_path)
    source_ids = []
    for name in ("collision/source", "collision-source"):
        created = client.post("/api/recipes/sources", json={"name": name, "url": repo})
        assert created.status_code == 201, created.text
        source_ids.append(created.json()["id"])
        synced = client.post(f"/api/recipes/sources/{source_ids[-1]}/sync")
        assert synced.status_code == 200, synced.text

    with S() as db:
        mirror_dirs = [db.get(RecipeSource, source_id).mirror_dir for source_id in source_ids]
    assert len(set(mirror_dirs)) == 2
    paths = [tmp_path / "mirror" / mirror_dir for mirror_dir in mirror_dirs]
    assert all(path.is_dir() for path in paths)

    deleted = client.delete(f"/api/recipes/sources/{source_ids[0]}")
    assert deleted.status_code == 200, deleted.text
    assert not paths[0].exists()
    assert paths[1].is_dir()


def test_source_rejects_blank_url(env):
    client, _ = env
    response = client.post("/api/recipes/sources/discover", json={"url": "   "})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "recipe_source_invalid"


def test_source_probe_filters_option_like_branch(env, monkeypatch):
    client, _ = env
    output = (
        "ref: refs/heads/-upload-pack=bad\tHEAD\n"
        "a" * 40 + "\trefs/heads/-upload-pack=bad\n"
        + "b" * 40 + "\trefs/heads/main\n"
    )
    monkeypatch.setattr(
        recipe_source,
        "_git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ""),
    )
    response = client.post(
        "/api/recipes/sources/discover", json={"url": "https://example.invalid/repo.git"}
    )
    assert response.status_code == 200
    assert response.json() == {"default_branch": "main", "branches": ["main"]}


def test_source_probe_redacts_url_credentials(env, monkeypatch):
    client, _ = env
    url = "https://secret-token@example.invalid/repo.git"
    monkeypatch.setattr(
        recipe_source,
        "_git",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 128, "", f"fatal: unable to access '{url}': denied"
        ),
    )
    response = client.post("/api/recipes/sources/discover", json={"url": url})
    assert response.status_code == 400
    assert "secret-token" not in response.text


# ---------- 同步 + 目录 + 安装 ----------


def test_sync_catalog_install_full_flow(env, tmp_path):
    client, S = env
    repo = _make_repo(tmp_path)
    source_id = _add_source(client, repo)

    r = client.post(f"/api/recipes/sources/{source_id}/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "synced"
    assert body["last_commit"]
    assert body["recipe_count"] == 1

    r = client.get(f"/api/recipes/sources/{source_id}/catalog")
    assert r.status_code == 200, r.text
    cat = r.json()
    assert len(cat["items"]) == 1
    item = cat["items"][0]
    assert item["id"] == "test-model"
    assert item["version"] == "1.2.3"
    assert item["dtype"] == "nvfp4"
    assert item["description_en"] == "English description"  # 目录双语透传
    # 目录不做「已安装」回显（下载即独立）

    # 安装（第一次）
    r = client.post("/api/recipes/install",
                    json={"source_id": source_id, "path": item["path"]})
    assert r.status_code == 201, r.text
    rec1 = r.json()
    # 默认 lang=zh：本地单语言快照（中文名 + 版本后缀）；配方独立，无来源字段
    assert rec1["name"] == "Test-Model（专属镜像） (v1.2.3)"
    assert "origin_source_id" not in rec1
    assert "installed_version" not in rec1
    assert rec1["node_count"] == 2          # 固定拓扑（确切节点数）随安装入库
    assert rec1["tensor_parallel"] == 2
    # auto_fix：command 用 bash 且缺 entrypoint -> 自动补
    assert "entrypoint: []" in rec1["compose_template"]

    # 安装（第二次）-> 永不覆盖、每次新行（独立配方）
    r = client.post("/api/recipes/install",
                    json={"source_id": source_id, "path": item["path"]})
    assert r.status_code == 201
    rec2 = r.json()
    assert rec2["id"] != rec1["id"]
    with S() as db:
        rows = db.query(Recipe).all()
        assert len(rows) == 2
        vs = {v["key"]: v for v in rows[0].variables}
        assert vs["MASTER_ADDR"]["auto"] == "head_roce_ip"
        assert vs["MASTER_PORT"]["source"] == "user"
        # 本地为单语言快照：label 取当前语言，且不保留任何 *_en 并列字段
        assert vs["MASTER_ADDR"]["label"] == "Head 节点"
        assert "label_en" not in vs["MASTER_ADDR"]

    # README
    r = client.get(f"/api/recipes/sources/{source_id}/readme",
                   params={"path": item["readme"]})
    assert r.status_code == 200
    assert "Test-Model" in r.json()["content"]

    # 路径穿越防御
    r = client.get(f"/api/recipes/sources/{source_id}/readme", params={"path": "../index.json"})
    assert r.status_code == 400


def test_sync_without_manifest_is_failure(env, tmp_path):
    """manifest 驱动：仓库缺少 recipes/index.json 视为同步失败（不做整树扫描回退）。"""
    client, _ = env
    repo = _make_repo(tmp_path, with_manifest=False)
    source_id = _add_source(client, repo)
    r = client.post(f"/api/recipes/sources/{source_id}/sync")
    assert r.status_code == 422, r.text
    src = client.get("/api/recipes/sources").json()[0]
    assert src["status"] == "failed"
    assert "recipes/index.json" in src["error"]


def test_sync_does_not_write_local_recipes(env, tmp_path):
    """同步只刷镜像目录，绝不动本地 recipes 表。"""
    client, S = env
    repo = _make_repo(tmp_path)
    source_id = _add_source(client, repo)
    client.post(f"/api/recipes/sources/{source_id}/sync")
    with S() as db:
        assert db.query(Recipe).count() == 0


def test_install_localizes_to_current_lang(env, tmp_path):
    """安装按 lang 本地化为单语言快照：en 取 *_en，且剥离所有 *_en 并列字段。"""
    client, _ = env
    repo = _make_repo(tmp_path)
    source_id = _add_source(client, repo)
    client.post(f"/api/recipes/sources/{source_id}/sync")
    path = "models/test-model/recipe/fireworks.recipe.json"

    # 英文用户安装 -> 英文名 + 英文变量 label，无 _en 残留
    r = client.post("/api/recipes/install",
                    json={"source_id": source_id, "path": path, "lang": "en"})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["name"] == "Test-Model (dedicated image) (v1.2.3)"
    vm = {v["key"]: v for v in rec["variables"]}
    assert vm["MASTER_ADDR"]["label"] == "Head node"
    assert "label_en" not in vm["MASTER_ADDR"]
    assert "help_en" not in vm["MASTER_ADDR"]

    # 中文用户安装 -> 回退主语言（zh），且不显示英文
    r2 = client.post("/api/recipes/install",
                     json={"source_id": source_id, "path": path, "lang": "zh"})
    assert r2.status_code == 201
    vm2 = {v["key"]: v for v in r2.json()["variables"]}
    assert r2.json()["name"] == "Test-Model（专属镜像） (v1.2.3)"
    assert vm2["MASTER_ADDR"]["label"] == "Head 节点"


# ---------- 固定拓扑（确切节点数）发布校验 ----------


@pytest.mark.anyio
async def test_publish_exact_node_count_required():
    """配方声明确切节点数（node_count=2）时，发布节点数必须恰好匹配（不做 min/max 比较）。"""
    from sqlalchemy.orm import sessionmaker
    from fastapi import HTTPException
    from app.models import Cluster, ClusterNode, Node
    from app.routers.tasks import create_task
    from app.schemas import TaskCreate

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    try:
        db.add_all([
            Cluster(id=1, name="cl", network_type="roce"),
            Node(id=1, name="n1", ip="10.0.0.1"),
            Node(id=2, name="n2", ip="10.0.0.2"),
            # 配方级固定拓扑：node_count=2（非变量 min/max）
            Recipe(
                id=1, name="fixed-2", image="x/y:1",
                node_count=2, tensor_parallel=2,
                compose_template="services:\n  x:\n    image: x/y:1\n",
                variables=[{"key": "NODES_TOTAL", "type": "int", "source": "cluster",
                            "auto": "nodes_total", "required": True}],
            ),
            ClusterNode(cluster_id=1, node_id=1, net_index=1),
            ClusterNode(cluster_id=1, node_id=2, net_index=2),
        ])
        db.commit()

        # 仅 1 台节点 -> 固定 2 节点拓扑必须拒绝
        req = TaskCreate(
            name="t1", recipe_id=1, cluster_id=1,
            nodes=[{"node_id": 1, "role": "head", "node_rank": 0}], variables={},
        )
        with pytest.raises(HTTPException) as e:
            await create_task(req, db)
        assert e.value.status_code == 400
        assert "恰好" in str(e.value.detail)
    finally:
        db.close()
