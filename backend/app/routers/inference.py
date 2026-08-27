"""推理统计：完整读取窗口累计快照，服务端差分并聚合为有界图表序列。"""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import InferenceSample, Task
from ..services.inference_aggregation import aggregate_inference_samples

router = APIRouter(tags=["inference"])


@router.get("/api/inference/metrics")
def inference_metrics(
    db: Session = Depends(get_db),
    from_ts: Annotated[float, Query(ge=0)] = 0.0,
    to_ts: Annotated[float | None, Query(ge=0)] = None,
    task_id: Annotated[int | None, Query(ge=1)] = None,
    max_points: Annotated[int, Query(ge=1, le=5000)] = 1440,
):
    """返回窗口摘要和按任务/节点独立聚合的图表点。

    `max_points` 是每条序列的最大图表点数，不是源数据行限制。窗口内所有累计
    快照均参与差分和聚合；额外读取窗口前最后一份快照作为差分基线。
    """
    to = to_ts if to_ts is not None else time.time()
    frm = min(from_ts, to)
    filters = [InferenceSample.ts >= frm, InferenceSample.ts <= to]
    if task_id is not None:
        filters.append(InferenceSample.task_id == task_id)
    window_rows = (
        db.query(InferenceSample)
        .filter(*filters)
        .order_by(InferenceSample.task_id, InferenceSample.node_id, InferenceSample.ts)
        .all()
    )

    # 每条实际出现的序列额外取窗口前最后一点，避免首个窗口内样本只能当基线而
    # 丢掉跨越窗口边界的计数器增量。该查询用 (task_id, node_id, ts) 索引定位。
    window_keys = (
        db.query(InferenceSample.task_id, InferenceSample.node_id)
        .filter(*filters)
        .distinct()
        .subquery()
    )
    baseline_query = (
        db.query(
            InferenceSample.task_id,
            InferenceSample.node_id,
            func.max(InferenceSample.ts).label("max_ts"),
        )
        .join(
            window_keys,
            and_(
                InferenceSample.task_id == window_keys.c.task_id,
                InferenceSample.node_id == window_keys.c.node_id,
            ),
        )
        .filter(InferenceSample.ts < frm)
    )
    baseline_ts = baseline_query.group_by(
        InferenceSample.task_id, InferenceSample.node_id
    ).subquery()
    baselines = (
        db.query(InferenceSample)
        .join(
            baseline_ts,
            and_(
                InferenceSample.task_id == baseline_ts.c.task_id,
                InferenceSample.node_id == baseline_ts.c.node_id,
                InferenceSample.ts == baseline_ts.c.max_ts,
            ),
        )
        .all()
    )

    task_ids = {row.task_id for row in window_rows}
    task_names = (
        {
            row.id: row.name
            for row in db.query(Task.id, Task.name).filter(Task.id.in_(task_ids)).all()
        }
        if task_ids
        else {}
    )
    return aggregate_inference_samples(
        [*baselines, *window_rows],
        from_ts=frm,
        to_ts=to,
        max_points=max_points,
        task_names=task_names,
    )
