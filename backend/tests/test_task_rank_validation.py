"""任务级 rank 校验：唯一且覆盖 0..N-1。

分布式要求 rank 恰好为 0..N-1（vLLM --node-rank 直接透传）。此前后端只校验
rank 唯一，重复是挡住了，但留洞（如 2 台节点用 rank 0 和 2）或越界（rank≥N）
会通过创建/预览校验，到节点侧才以分布式启动失败暴露。此测试守住创建与预览
共用的 validate_node_ranks：拒绝重复与越界 rank（等价于要求 rank 连续）。
"""

from types import SimpleNamespace

import pytest
from app.errors import Code
from app.routers.tasks import validate_node_ranks
from app.schemas import TaskNodeAssignment
from fastapi import HTTPException


def a(node_id: int, rank: int, role: str = "worker") -> TaskNodeAssignment:
    return TaskNodeAssignment(node_id=node_id, role=role, node_rank=rank)


def test_valid_contiguous_ranks_accepted():
    # head=0, worker 1..N-1：唯一且连续，必须通过
    validate_node_ranks([a(1, 0, "head"), a(2, 1), a(3, 2), a(4, 3)])


def test_duplicate_rank_rejected():
    with pytest.raises(HTTPException) as e:
        validate_node_ranks([a(1, 0, "head"), a(2, 1), a(3, 1)])
    assert e.value.status_code == 400
    assert e.value.detail.get("code") == Code.TASK_RANK_TAKEN


def test_rank_out_of_range_rejected():
    # 3 台节点但出现 rank 3：越界，等价于留洞（0..2 被 0,1 之外的值挤占）
    with pytest.raises(HTTPException) as e:
        validate_node_ranks([a(1, 0, "head"), a(2, 1), a(3, 3)])
    assert e.value.status_code == 400
    assert e.value.detail.get("code") == Code.TASK_RANK_OUT_OF_RANGE


def test_rank_gap_rejected():
    # 2 台节点用 rank 0 和 2：唯一但留洞，同样必须在发布前拒绝
    with pytest.raises(HTTPException) as e:
        validate_node_ranks([a(1, 0, "head"), a(2, 2)])
    assert e.value.status_code == 400
    assert e.value.detail.get("code") == Code.TASK_RANK_OUT_OF_RANGE


def test_negative_rank_rejected():
    # schema ge=0 已拦 pydantic 层，此处守函数自身的防御性检查（非 pydantic 对象）
    validate_node_ranks([a(1, 0, "head"), a(2, 1)])  # sanity
    with pytest.raises(HTTPException) as e:
        validate_node_ranks([
            SimpleNamespace(node_id=1, node_rank=0),
            SimpleNamespace(node_id=2, node_rank=-1),
        ])
    assert e.value.status_code == 400
    assert e.value.detail.get("code") == Code.TASK_RANK_OUT_OF_RANGE
