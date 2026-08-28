"""任务名校验：任务名 = docker compose 项目名，节点 Docker Compose v5 只允许
`^[a-z0-9][a-z0-9_-]*$`（小写字母/数字/'-'/'_'，以字母或数字开头）。

此前 backend 校验允许点与大小写（如 glm5.3-flash-nv / GLM-5.3-...），
发布到节点后才被 compose 拒绝（`docker compose up` rc!=0 -> agent 502，
控制面表现为 "Server error '502 Bad Gateway'"）。此测试守住新规则：
带点/大写/空格/非法起始字符的任务名一律在创建前以 400 TASK_NAME_INVALID 拒绝。
"""

import pytest
from app.errors import Code
from app.routers.tasks import _validate_task_name
from fastapi import HTTPException

GOOD = [
    "glm53-flash-nv",
    "a",
    "task-1",
    "dsv4f_nv01",
    "0abc",
    "a_b-c9",
    "x",
]
BAD = [
    "",
    ".",
    "..",
    "glm5.3-flash-nv",   # 点（本次 502 的直接原因）
    "Task",              # 大写
    "GLM-5.3-Flash",     # 大写 + 点
    "a b",               # 空格
    "-abc",              # 以非法字符开头
    "_abc",
    "a.b",
    "a/b",
    "a..b",
]


def test_valid_task_names_accepted():
    for name in GOOD:
        assert _validate_task_name(name) == name


def test_invalid_task_names_rejected():
    for name in BAD:
        with pytest.raises(HTTPException) as e:
            _validate_task_name(name)
        assert e.value.status_code == 400
        assert e.value.detail.get("code") == Code.TASK_NAME_INVALID
