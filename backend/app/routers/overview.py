"""总览统计。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..models import Cluster, MetricSample, Node, Recipe, Task

router = APIRouter(tags=["overview"])


@router.get("/api/overview", response_model=schemas.OverviewOut)
def overview(db: Session = Depends(get_db)):
    nodes = db.query(Node).all()
    tasks = db.query(Task).all()
    online = [n for n in nodes if n.agent_status == "online"]
    gpu_total = sum(len((n.hardware_info or {}).get("gpus", [])) for n in nodes)

    util_sum = 0.0
    util_count = 0
    mem_used = mem_total = 0
    for n in online:
        latest = (
            db.query(MetricSample)
            .filter(MetricSample.node_id == n.id)
            .order_by(MetricSample.ts.desc())
            .first()
        )
        if latest:
            g = (latest.data or {}).get("gpu") or {}
            if g.get("utilization") is not None:
                util_sum += g["utilization"]
                util_count += 1
            mem_used += g.get("mem_used", 0)
            mem_total += g.get("mem_total", 0)

    return schemas.OverviewOut(
        nodes_total=len(nodes),
        nodes_online=len(online),
        clusters_total=db.query(Cluster).count(),
        recipes_total=db.query(Recipe).count(),
        tasks_total=len(tasks),
        tasks_running=sum(1 for t in tasks if t.status == "running"),
        tasks_paused=sum(1 for t in tasks if t.status == "paused"),
        gpu_aggregate={
            "total": gpu_total,
            "utilization": round(util_sum / util_count, 1) if util_count else None,
            "mem_used": mem_used,
            "mem_total": mem_total,
        },
    )
