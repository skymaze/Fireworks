"""配方管理：CRUD、复制、渲染预览、配方源（目录）同步与安装。"""

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..errors import Code, api_error
from ..models import Cluster, Node, Recipe, RecipeSource
from ..services import recipe_import, recipe_render, recipe_source

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


def get_recipe_or_404(db: Session, recipe_id: int) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise api_error(404, Code.RECIPE_NOT_FOUND, "配方不存在")
    return recipe


def get_source_or_404(db: Session, source_id: int) -> RecipeSource:
    source = db.get(RecipeSource, source_id)
    if not source:
        raise api_error(404, Code.RECIPE_SOURCE_NOT_FOUND, "配方源不存在")
    return source


class PreviewRequest(BaseModel):
    cluster_id: int
    # 任务级 head/worker/rank 分配（与发布一致，仅渲染不落库）
    nodes: list[schemas.TaskNodeAssignment] = []
    variables: dict[str, str] = {}


@router.get("", response_model=list[schemas.RecipeOut])
def list_recipes(db: Session = Depends(get_db)):
    return db.query(Recipe).order_by(Recipe.id).all()


@router.post("", response_model=schemas.RecipeOut, status_code=201)
def create_recipe(req: schemas.RecipeCreate, db: Session = Depends(get_db)):
    if db.query(Recipe).filter(Recipe.name == req.name).first():
        raise api_error(409, Code.RECIPE_NAME_EXISTS, "同名配方已存在")
    recipe = Recipe(
        name=req.name,
        description=req.description,
        image=req.image,
        compose_template=req.compose_template,
        variables=[v.model_dump() for v in req.variables],
        node_count=req.nodes,  # 配方级固定拓扑（确切节点数）
    )
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    return recipe


# ---------- 配方源（目录）：读 FireworksRecipes git，只刷镜像；安装才写 recipes ----------


@router.get("/sources", response_model=list[schemas.RecipeSourceOut])
def list_sources(db: Session = Depends(get_db)):
    return db.query(RecipeSource).order_by(RecipeSource.id).all()


@router.post("/sources", response_model=schemas.RecipeSourceOut, status_code=201)
def create_source(req: schemas.RecipeSourceCreate, db: Session = Depends(get_db)):
    recipe_source.validate_url(req.url)
    if db.query(RecipeSource).filter(RecipeSource.name == req.name).first():
        raise api_error(409, Code.RECIPE_SOURCE_NAME_EXISTS, "同名配方源已存在")
    source = RecipeSource(name=req.name, url=req.url, branch=req.branch or "main")
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db)):
    source = get_source_or_404(db, source_id)
    # 镜像目录保留在磁盘（同名源可复用），只删除记录与溯源关系保持（安装行仍可发布）。
    db.delete(source)
    db.commit()
    return {"ok": True}


@router.post("/sources/{source_id}/sync", response_model=schemas.RecipeSourceOut)
def sync_source(source_id: int, db: Session = Depends(get_db)):
    """同步 = 浅克隆/拉取镜像目录（manifest 驱动），绝不写 recipes 表。"""
    source = get_source_or_404(db, source_id)
    recipe_source.sync_source(db, source)
    db.refresh(source)
    return source


@router.get("/sources/{source_id}/catalog", response_model=schemas.CatalogOut)
def recipe_catalog(source_id: int, db: Session = Depends(get_db)):
    """目录：manifest 条目 + git 更新时间 + 本地已安装实例。"""
    source = get_source_or_404(db, source_id)
    return recipe_source.catalog(db, source)


@router.get("/sources/{source_id}/readme")
def source_readme(source_id: int, path: str, db: Session = Depends(get_db)):
    """目录条目的 README 文档（原始 markdown）。"""
    source = get_source_or_404(db, source_id)
    content = recipe_source.read_readme(source, path)
    return {"path": path, "content": content}


@router.post("/install", response_model=schemas.RecipeOut, status_code=201)
def install_recipe(req: schemas.RecipeInstallIn, db: Session = Depends(get_db)):
    """安装 = 用户显式行为：读镜像 recipe.json -> 本地化（按当前语言 lang 快照）-> 新建行。
    每次必新建，绝不覆盖；本地只存用户当前使用的一种语言。"""
    source = get_source_or_404(db, req.source_id)
    return recipe_source.install(db, source, req.path, lang=req.lang)


@router.get("/{recipe_id}", response_model=schemas.RecipeOut)
def get_recipe(recipe_id: int, db: Session = Depends(get_db)):
    return get_recipe_or_404(db, recipe_id)


@router.patch("/{recipe_id}", response_model=schemas.RecipeOut)
def update_recipe(recipe_id: int, req: schemas.RecipeUpdate, db: Session = Depends(get_db)):
    recipe = get_recipe_or_404(db, recipe_id)
    data = req.model_dump(exclude_unset=True)  # variables 已由 model_dump 转为 dict 列表
    if "nodes" in data:
        data["node_count"] = data.pop("nodes")  # 请求字段 nodes -> 存储列 node_count
    if "name" in data and db.query(Recipe).filter(
        Recipe.name == data["name"], Recipe.id != recipe_id
    ).first():
        raise api_error(409, Code.RECIPE_NAME_EXISTS, "同名配方已存在")
    for k, v in data.items():
        setattr(recipe, k, v)
    db.commit()
    db.refresh(recipe)
    return recipe


@router.delete("/{recipe_id}")
def delete_recipe(recipe_id: int, db: Session = Depends(get_db)):
    recipe = get_recipe_or_404(db, recipe_id)
    db.delete(recipe)
    db.commit()
    return {"ok": True}


@router.post("/{recipe_id}/duplicate", response_model=schemas.RecipeOut, status_code=201)
def duplicate_recipe(recipe_id: int, db: Session = Depends(get_db)):
    recipe = get_recipe_or_404(db, recipe_id)
    name = f"{recipe.name} (副本)"
    if db.query(Recipe).filter(Recipe.name == name).first():
        name = f"{recipe.name} (副本 {int(time.time())})"
    new = Recipe(
        name=name,
        description=recipe.description,
        image=recipe.image,
        compose_template=recipe.compose_template,
        variables=recipe.variables,
        is_seed=False,
        node_count=recipe.node_count,
        tensor_parallel=recipe.tensor_parallel,
    )
    db.add(new)
    db.commit()
    db.refresh(new)
    return new


@router.get("/{recipe_id}/export")
def export_recipe(recipe_id: int, db: Session = Depends(get_db)):
    """导出配方为 JSON（可再导入）。"""
    recipe = get_recipe_or_404(db, recipe_id)
    return {
        "name": recipe.name,
        "description": recipe.description,
        "image": recipe.image,
        "compose_template": recipe.compose_template,
        "variables": recipe.variables,
        "nodes": recipe.node_count,  # 固定拓扑（确切节点数），导出/再导入保留
        "tensor_parallel": recipe.tensor_parallel,
    }


class RecipeImport(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    image: str | None = None
    compose_template: str
    variables: list[schemas.VariableDef] = []
    # 固定拓扑：确切的节点数（None=不固定）
    nodes: int | None = Field(None, ge=1, le=64)
    # 可选：以现有配方为基创建
    duplicate_of: int | None = None


@router.post("/import", response_model=schemas.RecipeOut, status_code=201)
def import_recipe(req: RecipeImport, db: Session = Depends(get_db)):
    """导入配方（JSON）。name 冲突时自动追加序号；compose 模板做安全自适应修正。"""
    compose_template, notices = recipe_import.auto_fix_compose(req.compose_template)
    name = req.name
    if db.query(Recipe).filter(Recipe.name == name).first():
        name = f"{name} (导入 {int(time.time())})"
    recipe = Recipe(
        name=name,
        description=req.description,
        image=req.image,
        compose_template=compose_template,
        variables=[v.model_dump() for v in req.variables],
        is_seed=False,
        node_count=req.nodes,  # 固定拓扑（确切节点数）
    )
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    out = schemas.RecipeOut.model_validate(recipe)
    if notices:
        out.import_notice = "\n".join(notices)
    return out


@router.post("/{recipe_id}/preview")
def preview_recipe(recipe_id: int, req: PreviewRequest, db: Session = Depends(get_db)):
    """发布前预览：按集群 + 任务级 head/worker/rank 分配渲染逐节点 env（不落库）。"""
    recipe = get_recipe_or_404(db, recipe_id)
    cluster = db.get(Cluster, req.cluster_id)
    if not cluster:
        raise api_error(404, Code.CLUSTER_NOT_FOUND, "集群不存在")
    member_map = {m.node_id: m for m in cluster.members}

    assignments = []
    for a in req.nodes:
        node = db.get(Node, a.node_id)
        if not node or node.id not in member_map:
            raise api_error(400, Code.NODE_NOT_IN_CLUSTER,
                            f"节点 {a.node_id} 不在所选集群中", params={"id": a.node_id})
        assignments.append((node, a.role, a.node_rank))

    try:
        rendered = recipe_render.render_task(recipe, cluster, assignments, req.variables, "preview")
    except recipe_render.RenderError as e:
        raise HTTPException(422, str(e)) from e
    return rendered
