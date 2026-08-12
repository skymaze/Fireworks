"""推理统计查询：单接口返回原始累计快照，支持时间范围 / 任务过滤 / 增量拉取。"""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import InferenceSample, Task

router = APIRouter(tags=["inference"])


@router.get("/api/inference/samples")
def inference_samples(
    db: Session = Depends(get_db),
    from_ts: Annotated[float, Query(ge=0)] = 0.0,
    to_ts: Annotated[float | None, Query(ge=0)] = None,
    task_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100000)] = 5000,
):
    """原始推理统计样本（未差分，由前端自行差分/绘图）。

    - ``from_ts`` / ``to_ts``：时间范围（秒时间戳），``to_ts`` 缺省为当前时间；
    - ``task_id`` 可选：只返回该任务；缺省返回全部任务（总览按任务 id 聚类）；
    - **增量拉取**：``from_ts`` 传上次最后样本的 ``ts`` 即可拿到新样本；
    - ``limit`` 超限按步长降采样（保留最新、含首点），仅用于减负；
      返回行按 ``ts`` 升序，含原始累计计数器 / KV gauge / 直方图。
    """
    now = time.time()
    to = to_ts if to_ts is not None else now
    frm = min(from_ts, to)
    query = (
        db.query(InferenceSample)
        .filter(InferenceSample.ts >= frm, InferenceSample.ts <= to)
        .order_by(InferenceSample.ts)
    )
    if task_id is not None:
        query = query.filter(InferenceSample.task_id == task_id)
    rows = query.all()
    if len(rows) > limit:
        # 等距保留首尾（必须含最新样本，作为增量拉取的 from_ts 锚点）
        last = len(rows) - 1
        idx = sorted({round(i * last / (limit - 1)) for i in range(limit)})
        rows = [rows[i] for i in idx]
    # 任务名：前端按任务聚类绘图需要名称；一次性取全量避免 N+1
    task_names = {t.id: t.name for t in db.query(Task.id, Task.name).all()}
    return [
        {
            "ts": r.ts,
            "task_id": r.task_id,
            "task_name": task_names.get(r.task_id),
            "node_id": r.node_id,
            "model_name": r.model_name,
            "data": r.data,
        }
        for r in rows
    ]
