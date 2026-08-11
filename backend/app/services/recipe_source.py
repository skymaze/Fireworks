"""配方源（FireworksRecipes git 仓库）管理：同步镜像 + 目录 + 安装。

分层规矩（与 recipes 表隔离）：
- recipes 表 = 本地已安装（可编辑/发布）。用户显式「安装」才新建行，**绝不覆盖/原地更新**，
  因此同一目录配方可共存多个版本；用户自建/手导配方（origin_* 为空）永不触碰。
- 配方源镜像目录 = 只读目录（git 浅克隆/拉取产物），同步只刷这里，不写 recipes。
- 目录数据 = 仓库根 `recipes/index.json`（manifest，等同 vllm.ai 的 /models.json）。
  不做整树扫描回退：诊断/避免误抓仓库里非配方的 .json（如基准结果）。

git 用浅克隆（--depth 1 --single-branch）：目录视图只需要最新配方元数据与文档。
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import config, schemas
from ..errors import Code, api_error
from ..models import Recipe, RecipeSource
from . import recipe_import, recipe_render

logger = logging.getLogger(__name__)

MANIFEST_RELPATH = "recipes/index.json"

# 禁止的协议：镜像只能是普通 git 仓库（https/http/本地路径），
# 避免把 file:// 等文件访问能力经配方源暴露到任意路径。
_BLOCK_SCHEMES = re.compile(r"^\s*(file|data|gopher|dict):", re.IGNORECASE)

# 单进程部署内每个源只允许一个 git 操作。数据库中的 ``syncing`` 是用户可见状态，
# 不能单独充当锁：进程退出后它会残留，而此处的运行态锁会随进程释放。
_SYNC_LOCKS: dict[int, threading.Lock] = {}
_SYNC_LOCKS_GUARD = threading.Lock()


def _sync_lock(source_id: int) -> threading.Lock:
    with _SYNC_LOCKS_GUARD:
        return _SYNC_LOCKS.setdefault(source_id, threading.Lock())


def _slug(raw: str) -> str:
    """URL/名称 -> 镜像子目录名（安全字符集）。"""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return s[:120] or "default"


def mirror_path(source: RecipeSource) -> Path:
    if not source.mirror_dir:
        raise api_error(422, Code.CATALOG_NOT_SYNCED, "配方源尚未同步")
    return Path(config.RECIPE_SRC_DIR) / source.mirror_dir


def validate_url(url: str) -> None:
    if not url.strip() or _BLOCK_SCHEMES.match(url) or url.lstrip().startswith("-"):
        raise api_error(400, Code.RECIPE_SOURCE_INVALID, "配方源地址不支持该协议（仅限 http/https/本地路径）")


def _git(args: list[str], cwd: Path | None = None,
          timeout: int | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")  # 免交互（私库需已配置凭据，绝不弹出提示卡死）
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
        timeout=timeout or config.RECIPE_SYNC_TIMEOUT,
    )


def discover_branches(url: str) -> dict[str, str | list[str]]:
    """读取远端分支与 HEAD 默认分支，不克隆仓库、不修改本地镜像。"""
    url = url.strip()
    validate_url(url)
    try:
        result = _git(
            ["ls-remote", "--symref", url, "HEAD", "refs/heads/*"],
            timeout=min(config.RECIPE_SYNC_TIMEOUT, 60),
        )
    except subprocess.TimeoutExpired as exc:
        raise api_error(
            504, Code.RECIPE_SOURCE_PROBE_FAILED,
            "读取配方源分支超时，请检查仓库地址和网络连接",
            details=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise api_error(
            400, Code.RECIPE_SOURCE_PROBE_FAILED,
            f"读取配方源分支失败：{exc}", details=str(exc),
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git ls-remote 执行失败").strip()[-2000:]
        # 仓库 URL 可能内嵌访问令牌；错误会返回 WebUI，不得回显凭据。
        detail = detail.replace(url, "<repository>")
        detail = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", detail)
        raise api_error(
            400, Code.RECIPE_SOURCE_PROBE_FAILED,
            f"无法读取配方源分支：{detail}", details=detail,
        )

    default_branch = ""
    branches: set[str] = set()
    for line in result.stdout.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            default_branch = line.removeprefix("ref: refs/heads/").removesuffix("\tHEAD")
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            branch = parts[1].removeprefix("refs/heads/")
            # 后续 branch 会作为 git fetch 的 refspec；限制长度并拒绝可能被
            # git 解释为选项的前导连字符。其它合法字符由远端 ref 保证。
            if len(branch) <= 128 and not branch.startswith("-"):
                branches.add(branch)
    if default_branch and len(default_branch) <= 128 and not default_branch.startswith("-"):
        branches.add(default_branch)
    elif default_branch:
        default_branch = ""
    if not branches:
        raise api_error(
            422, Code.RECIPE_SOURCE_PROBE_FAILED,
            "仓库没有可用分支，请确认仓库已经包含至少一次提交",
        )
    ordered = sorted(branches)
    if not default_branch:
        default_branch = "main" if "main" in branches else (
            "master" if "master" in branches else ordered[0]
        )
    return {"default_branch": default_branch, "branches": ordered}


def require_branch(url: str, branch: str) -> None:
    """确认分支仍存在，避免保存后到同步阶段才暴露拼写错误。"""
    info = discover_branches(url)
    if branch not in info["branches"]:
        raise api_error(
            422, Code.RECIPE_SOURCE_BRANCH_NOT_FOUND,
            f"配方源中不存在分支 {branch}", params={"branch": branch},
        )


def delete_mirror(source: RecipeSource) -> None:
    """删除配方源的只读镜像；严格限制目标必须位于 RECIPE_SRC_DIR 内。"""
    if not source.mirror_dir:
        return
    root = Path(config.RECIPE_SRC_DIR).resolve()
    dest = (root / source.mirror_dir).resolve()
    if dest == root or not dest.is_relative_to(root):
        raise api_error(500, Code.RECIPE_SOURCE_INVALID, "配方源镜像路径异常，已拒绝删除")
    try:
        if (root / source.mirror_dir).is_symlink():
            (root / source.mirror_dir).unlink()
        elif dest.exists():
            shutil.rmtree(dest)
    except OSError as exc:
        raise api_error(
            500, Code.RECIPE_SOURCE_INVALID,
            f"配方源镜像清理失败：{exc}", details=str(exc),
        ) from exc


def _resolve_within(mirror: Path, rel: str) -> Path:
    """把仓库内相对路径安全解析到镜像目录内（防 manifest/条目路径穿越）。"""
    p = (mirror / rel).resolve()
    if not p.is_relative_to(mirror.resolve()):
        raise api_error(400, Code.RECIPE_SOURCE_INVALID, f"路径越界: {rel}")
    return p


def _source_commit_time(source: RecipeSource) -> str | None:
    p = mirror_path(source)
    if not p.joinpath(".git").exists():
        return None
    try:
        r = _git(["-C", str(p), "log", "-1", "--format=%cI"], timeout=30)
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001 - 拿不到时间不影响目录功能
        return None


def recover_interrupted_syncs(db: Session) -> int:
    """启动时把上个进程遗留的同步标记转为可重试失败态。"""
    sources = db.query(RecipeSource).filter(RecipeSource.status == "syncing").all()
    for source in sources:
        source.status = "failed"
        source.error = "上次同步因服务重启或进程中断而未完成，请手动重试"
    if sources:
        db.commit()
        logger.warning("已恢复 %d 个中断的配方源同步状态", len(sources))
    return len(sources)


def sync_source(
    db: Session, source: RecipeSource, *, recover: bool = False
) -> dict:
    """浅克隆/拉取镜像仓库；``recover`` 可接管无本进程任务的遗留状态。"""
    lock = _sync_lock(source.id)
    if not lock.acquire(blocking=False):
        raise api_error(409, Code.RECIPE_SYNC_IN_PROGRESS, "该配方源正在同步中，请稍后再试")
    try:
        # 调用者取得 source 后可能已有另一个请求完成状态切换，必须强制刷新。
        db.refresh(source)
        if source.status == "syncing":
            if not recover:
                raise api_error(
                    409,
                    Code.RECIPE_SYNC_IN_PROGRESS,
                    "检测到未完成的同步状态，可点击“恢复同步”重新接管",
                    params={"recoverable": True},
                )
            logger.warning("手动接管配方源 %s 的遗留同步状态", source.name)
        return _sync_source_locked(db, source)
    finally:
        lock.release()


def _sync_source_locked(db: Session, source: RecipeSource) -> dict:
    """调用方持有源级运行态锁后执行实际 git 同步。"""
    if not source.mirror_dir:
        # slug 可能碰撞（如 a/b 与 a-b），加入数据库主键确保不同源永不共享、
        # 删除镜像时也不会误删另一个源。已有记录继续沿用原目录以保持兼容。
        source.mirror_dir = f"{source.id}-{_slug(source.name or source.url)}"
        db.commit()
    source.error = None
    source.status = "syncing"
    db.commit()
    dest = mirror_path(source)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # clone 超时或进程退出可能留下没有 .git 的半成品目录；git clone 不会覆盖
        # 非空目录，因此重试前必须安全清理，否则会永久失败。
        if dest.exists() and not dest.joinpath(".git").exists():
            delete_mirror(source)
        if not dest.joinpath(".git").exists():
            r = _git(["clone", "--depth", "1", "--single-branch",
                      "--branch", source.branch, source.url, str(dest)])
        else:
            # 浅目录 = 只读镜像，直接对齐远端分支（本地任何改动一律丢弃）。
            # 浅克隆/长期只读镜像下 refs/remotes/origin/<branch> 未必会被物化
            # （git 对已 up-to-date 的 shallow fetch 可能不刷新远端引用），此时
            # 硬写 "origin/<branch>" 会报 fatal: ambiguous argument，因此按顺序
            # 取 origin/<branch> -> FETCH_HEAD（fetch 总会记录远端 tip）。
            r = _git(["fetch", "--depth", "1", "origin", source.branch], cwd=dest)
            if r.returncode == 0:
                target = _git(["-C", str(dest), "rev-parse", "--verify", "--quiet",
                               f"origin/{source.branch}"], timeout=30).stdout.strip()
                if not target:
                    target = _git(["-C", str(dest), "rev-parse", "--verify", "--quiet",
                                   "FETCH_HEAD"], timeout=30).stdout.strip()
                if not target:
                    raise subprocess.CalledProcessError(
                        1, ["git"], output="",
                        stderr="无法确定远端目标提交（origin/<branch> 与 FETCH_HEAD 均不可用）")
                r = _git(["reset", "--hard", target], cwd=dest)
        if r.returncode != 0:
            raise subprocess.CalledProcessError(
                r.returncode if r.returncode else 1, ["git"], output=r.stdout, stderr=r.stderr
            )
        head = _git(["-C", str(dest), "rev-parse", "HEAD"], timeout=30)
        commit = head.stdout.strip()
        # manifest 读取：缺失/解析失败时 load_manifest 抛 HTTPException(422)
        count, _ = load_manifest(source)
        source.last_commit = commit
        source.recipe_count = count
        source.status = "synced"
        db.commit()
        logger.info("配方源 %s 同步完成: commit=%s recipes=%d", source.name, commit[:8], count)
        return {"commit": commit, "recipe_count": count, "branch": source.branch}
    except subprocess.TimeoutExpired as e:
        source.status, source.error = "failed", f"同步超时（{config.RECIPE_SYNC_TIMEOUT}s）: {e}"
        db.commit()
        raise api_error(500, Code.RECIPE_SYNC_FAILED, source.error) from e
    except subprocess.CalledProcessError as e:
        source.status, source.error = "failed", (e.stderr or str(e))[-2000:]
        db.commit()
        raise api_error(500, Code.RECIPE_SYNC_FAILED, f"配方源同步失败: {source.error}") from e
    except HTTPException:
        # 克隆成功但 manifest 缺失/解析失败：标记 failed 并透传（422 catalog_not_synced）
        source.status = "failed"
        source.error = f"仓库缺少 {MANIFEST_RELPATH}（manifest 驱动，不做整树扫描）"
        db.commit()
        raise
    except Exception as e:  # noqa: BLE001
        source.status, source.error = "failed", str(e)
        db.commit()
        raise api_error(500, Code.RECIPE_SYNC_FAILED, f"配方源同步失败: {e}") from e


def load_manifest(source: RecipeSource) -> tuple[int, dict]:
    """读取 manifest（recipes/index.json），返回 (条目数, 原始 dict)。"""
    p = _resolve_within(mirror_path(source), MANIFEST_RELPATH)
    if not p.is_file():
        raise api_error(422, Code.CATALOG_NOT_SYNCED,
                        f"配方源未同步或缺少 {MANIFEST_RELPATH}（manifest 驱动，请先同步）")
    try:
        manifest = json.loads(p.read_text(encoding="utf-8"))
        items = manifest.get("recipes") or []
        return len(items), manifest
    except (json.JSONDecodeError, OSError) as e:
        raise api_error(422, Code.CATALOG_NOT_SYNCED, f"{MANIFEST_RELPATH} 解析失败: {e}") from e


def _read_mirror_file(source: RecipeSource, rel: str, kind: str) -> str:
    mirror = mirror_path(source)
    if not mirror.joinpath(".git").exists():
        raise api_error(422, Code.CATALOG_NOT_SYNCED, "配方源尚未同步")
    p = _resolve_within(mirror, rel)
    if not p.is_file():
        raise api_error(404, Code.CATALOG_ITEM_NOT_FOUND, f"{kind} 不存在: {rel}")
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        raise api_error(500, Code.RECIPE_SYNC_FAILED, f"读取{kind}失败: {e}") from e


def read_recipe(source: RecipeSource, rel: str) -> dict:
    """读镜像里的 recipe.json，校验为 dict。"""
    raw = _read_mirror_file(source, rel, "配方")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise api_error(422, Code.RECIPE_IMPORT_INVALID, f"配方 JSON 解析失败: {e}") from e
    if not isinstance(data, dict) or not data.get("compose_template"):
        raise api_error(422, Code.RECIPE_IMPORT_INVALID, "配方缺少 compose_template 字段")
    return data


def read_readme(source: RecipeSource, rel: str) -> str:
    return _read_mirror_file(source, rel, "文档")


def catalog(db: Session, source: RecipeSource) -> schemas.CatalogOut:
    """manifest 驱动目录：条目 + git 更新时间。不做「已安装」回显/来源关联——
    配方一旦下载即是本地独立个体，用户可能已编辑。"""
    _, manifest = load_manifest(source)
    commit_time = _source_commit_time(source)

    items: list[schemas.CatalogItem] = []
    for it in manifest.get("recipes", []):
        path = it.get("path")
        if not path:
            continue
        # 条目引用的 recipe.json 必须真实存在（安装时才再次校验；目录预览用 manifest 元数据）
        items.append(schemas.CatalogItem(
            path=path,
            id=it.get("id") or Path(path).stem,
            name=it.get("name"),
            name_en=it.get("name_en"),
            provider=it.get("provider"),
            model=it.get("model"),
            version=it.get("version"),
            params=it.get("params"),
            dtype=it.get("dtype"),
            context_length=it.get("context_length"),
            modality=it.get("modality"),
            topology=it.get("topology"),
            image=it.get("image"),
            nodes=it.get("nodes"),
            tensor_parallel=it.get("tensor_parallel"),
            tags=list(it.get("tags") or []),
            description=it.get("description"),
            description_en=it.get("description_en"),
            readme=it.get("readme"),
            readme_en=it.get("readme_en"),
            updated_at=commit_time,
        ))

    return schemas.CatalogOut(
        source_id=source.id,
        source_name=source.name,
        commit=source.last_commit,
        recipe_count=len(items),
        items=items,
    )


def _localized_text(data: dict, key: str, lang: str) -> str | None:
    """按语言取配方双语字段：en 优先取 *_en，否则回退主语言（zh；主语言缺省亦回退 *_en）。"""
    primary = data.get(key)
    en = data.get(f"{key}_en")
    if lang == "en":
        return en or primary
    return primary or en


def _localize_variables(variables: list, lang: str) -> list:
    """把配方变量本地化为单语言快照：label/help 按 lang 选取（en 优先 *_en），
    其余字段原样保留；所有 *_en 并列字段剥离，本地只存一种语言。"""
    out: list[dict] = []
    for v in variables:
        if not isinstance(v, dict):
            continue
        nv: dict = {}
        for key, val in v.items():
            if key.endswith("_en"):
                continue
            if key in ("label", "help"):
                primary = val or ""
                other = v.get(f"{key}_en") or ""
                nv[key] = (other or primary) if lang == "en" else (primary or other)
            else:
                nv[key] = val
        out.append(nv)
    return out


def install(db: Session, source: RecipeSource, path: str, lang: str = "zh") -> schemas.RecipeOut:
    """从目录下载/安装配方到本地：**下载即成独立配方**（与来源无任何关联，
    用户可任意编辑；每次安装必新建行、绝不覆盖）。

    lang（zh|en）：按当前用户的语言把配方内容**本地化为单语言快照**入库——
    英文在配方提供 *_en 字段时取英文，否则回退主语言（zh）。本地只保存当前用户的
    一种语言，用户无需（也不应）来回切换语言；配方源侧仍保留完整双语。
    """
    data = read_recipe(source, path)
    version = data.get("version") or ""
    base = _localized_text(data, "name", lang) or Path(path).stem
    # 每次安装都是唯一新行：名字带版本（再冲突由 unique_name 补序号）
    candidate = f"{base} (v{version})" if version else base
    name = recipe_import.unique_name(db, candidate[:190])
    compose_template, notices = recipe_import.auto_fix_compose(data.get("compose_template", ""))
    # 变量自动键校验：source=cluster/node 必须用已知 auto 键，否则拒绝安装（fail-fast，
    # 避免用户装了个发布时静默丢变量的坏配方）
    variables = _localize_variables(data.get("variables") or [], lang)
    try:
        recipe_render.validate_recipe_auto_keys(variables)
    except recipe_render.RenderError as e:
        raise api_error(422, Code.RECIPE_INVALID_VARIABLES,
                        f"配方变量不合法: {e}", params={"path": path}, details=str(e)) from e
    recipe = Recipe(
        name=name,
        description=_localized_text(data, "description", lang),
        image=data.get("image"),
        compose_template=compose_template,
        variables=variables,
        is_seed=False,
        # 固定拓扑（确切节点数，配方级属性；发布时必须恰好匹配）
        node_count=data.get("nodes"),
        tensor_parallel=data.get("tensor_parallel"),
    )
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    out = schemas.RecipeOut.model_validate(recipe)
    if notices:
        out.import_notice = "\n".join(notices)
    logger.info("从配方源 %s 安装/下载配方 [%s]: %s (%s)", source.name, lang, name, path)
    return out
