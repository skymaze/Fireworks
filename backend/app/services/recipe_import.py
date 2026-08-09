"""配方导入通用工具：compose 模板安全修正 + 唯一命名。

导入（POST /recipes/import）与「配方源安装」（POST /recipes/install）共用：
安装只是把配方源镜像目录里的 recipe.json 走同一条安全通道落库。
"""

import re

from sqlalchemy.orm import Session

from ..models import Recipe


def auto_fix_compose(template: str) -> tuple[str, list[str]]:
    """对 compose 模板做安全自适应修正，返回 (修正后模板, 调整说明)。

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


def unique_name(db: Session, base: str) -> str:
    """返回在 recipes.name 唯一约束下可用的名字（冲突时追加序号）。"""
    name = base
    n = 2
    while db.query(Recipe).filter(Recipe.name == name).first():
        name = f"{base} ({n})"
        n += 1
    return name
