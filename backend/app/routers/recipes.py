"""配方管理：CRUD、复制、渲染预览。"""

import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..errors import Code, api_error
from ..models import Cluster, Node, Recipe
from ..services import recipe_render

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


def get_recipe_or_404(db: Session, recipe_id: int) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if not recipe:
        raise api_error(404, Code.RECIPE_NOT_FOUND, "配方不存在")
    return recipe


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
    )
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    return recipe


@router.get("/{recipe_id}", response_model=schemas.RecipeOut)
def get_recipe(recipe_id: int, db: Session = Depends(get_db)):
    return get_recipe_or_404(db, recipe_id)


@router.patch("/{recipe_id}", response_model=schemas.RecipeOut)
def update_recipe(recipe_id: int, req: schemas.RecipeUpdate, db: Session = Depends(get_db)):
    recipe = get_recipe_or_404(db, recipe_id)
    data = req.model_dump(exclude_unset=True)  # variables 已由 model_dump 转为 dict 列表
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
    }


class RecipeImport(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    image: str | None = None
    compose_template: str
    variables: list[schemas.VariableDef] = []
    # 可选：以现有配方为基创建
    duplicate_of: int | None = None


def _auto_fix_compose(template: str) -> tuple[str, list[str]]:
    """导入时对 compose 模板做安全自适应修正，返回 (修正后模板, 调整说明)。

    常见坑：镜像自带 ENTRYPOINT（如 Anemll 镜像的 `vllm serve`）时，模板若用
    `bash -lc` 作为 command，实际执行的是 `ENTRYPOINT bash -lc ...`，
    vllm 会把 `-lc` 当作未知参数而启动即崩（实际 E2E 中复现过）。
    修复：检测到 command 用 bash 且模板缺 entrypoint 时，自动补 `entrypoint: []`
    （与 seed 内置配方一致；对本身无 ENTRYPOINT 的镜像也无副作用）。
    /opt/env 为自建镜像布局路径，与分发镜像（如 /usr/local）不一致，仅提示不自动改。
    """
    notices: list[str] = []
    fixed = template
    uses_bash = re.search(r"^\s*command:\s*$", fixed, re.M) and bool(
        re.search(r"^[ \t]*-[ \t].*\bbash\b", fixed, re.M)
    )
    if uses_bash and "entrypoint:" not in fixed:
        m = re.search(r"^(\s*)image:.*$", fixed, re.M)
        if m:
            indent, seg = m.group(1), m.group(0)
            fixed = fixed.replace(seg, f"{seg}\n{indent}entrypoint: []", 1)
            notices.append(
                "已自动补充 entrypoint: []（检测到 bash command 但缺入口清空；"
                "避免镜像自带 ENTRYPOINT 把 bash -lc 当作参数而启动失败）"
            )
    if "/opt/env/" in fixed:
        notices.append(
            "检测到 /opt/env 自建镜像布局路径（如 /opt/env/bin/vllm、/opt/env/lib）。"
            "若使用 Anemll 等分发镜像，请确认并对齐路径（如 /usr/local/bin/vllm、/usr/local/cuda/lib64）。"
        )
    return fixed, notices


@router.post("/import", response_model=schemas.RecipeOut, status_code=201)
def import_recipe(req: RecipeImport, db: Session = Depends(get_db)):
    """导入配方（JSON）。name 冲突时自动追加序号；compose 模板做安全自适应修正。"""
    compose_template, notices = _auto_fix_compose(req.compose_template)
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
