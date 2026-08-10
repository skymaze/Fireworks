"""启动种子：创建默认配方源（FireworksRecipes git），recipes 表不再内置初始配方。

配方只来自配方源目录——用户显式「安装」才写入 recipes 表。首次启动若还没有任何
配方源，则创建默认源并 best-effort 同步一次（仅刷镜像目录，不写 recipes 表；
网络失败/仓库未公开时把错误记到源上，不阻断启动，界面可手动重试）。
"""

import logging

from sqlalchemy.orm import Session

from . import config
from .models import RecipeSource
from .services import recipe_source as recipe_source_svc

logger = logging.getLogger(__name__)


def seed_recipe_sources(db: Session) -> None:
    """确保默认配方源存在（仅当尚无任何源时创建并首次同步）。"""
    if db.query(RecipeSource).count() > 0:
        return
    source = RecipeSource(
        name="FireworksRecipes",
        url=config.RECIPE_DEFAULT_URL,
        branch=config.RECIPE_DEFAULT_BRANCH,
        mirror_dir=None,  # 首次同步时按 source.id + 名称生成唯一目录
    )
    db.add(source)
    db.commit()
    try:
        recipe_source_svc.sync_source(db, source)
        logger.info("默认配方源初始化同步完成")
    except Exception as e:  # noqa: BLE001 - 离线/仓库未公开时允许失败，界面可手动重试
        logger.warning("默认配方源首次同步失败（可在界面手动重试）: %s", e)
