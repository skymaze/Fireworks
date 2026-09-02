"""节点镜像管理（GET/DELETE /nodes/{id}/images）：SSH docker images / docker rmi。

无真实节点，直接调用 router 函数 + 假 DB + monkeypatch SSH，覆盖：
- 镜像列表：docker images --format json 逐行解析（大小转字节、悬挂 <none> 以 ID 为引用）；
- 镜像删除：成功 / docker 拒绝（409）/ 非法引用（400）/ SSH 失败（502）；
- 节点不存在 → 404。
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.errors import Code
from app.models import Node
from app.routers import nodes

# docker images --format '{{json .}}' 的典型输出（每行一个 JSON）
DOCKER_IMAGES_OUT = (
    '{"Containers":"N/A","CreatedSince":"2 days ago","CreatedAt":"2026-08-30 '
    '12:00:00 +0000 UTC","ID":"abc123def456","Repository":"anemll/dspark-vllm-gx10",'
    '"Tag":"0.3.4","Size":"1.52GB","SharedSize":"0B","UniqueSize":"1.52GB",'
    '"VirtualSize":"1.52GB","Labels":"","digest":""}\n'
    '{"Containers":"0","CreatedSince":"3 weeks ago","CreatedAt":"2026-08-10 '
    '12:00:00 +0000 UTC","ID":"fed654cba321","Repository":"<none>","Tag":"<none>",'
    '"Size":"234MB","SharedSize":"0B","UniqueSize":"234MB","VirtualSize":"234MB",'
    '"Labels":"","digest":""}\n'
    '{"Containers":"0","CreatedSince":"10 minutes ago","CreatedAt":"2026-09-02 '
    '09:00:00 +0000 UTC","ID":"11aa22bb33cc","Repository":"fireworks-models/deepseek",'
    '"Tag":"latest","Size":"0B","SharedSize":"0B","UniqueSize":"0B",'
    '"VirtualSize":"0B","Labels":"","digest":""}\n'
)


class _FakeDB:
    """内存假 DB：仅实现镜像路由用到的 get(Node, id) 与 commit。"""

    def __init__(self, *node_list: Node):
        self.by_id = {n.id: n for n in node_list}
        self.commits = 0

    def get(self, model, node_id):
        if model is Node:
            return self.by_id.get(node_id)
        return None

    def commit(self):
        self.commits += 1


def _node(nid: int, name: str) -> Node:
    return Node(id=nid, name=name, ip=f"10.0.0.{nid}", ssh_username="spark",
                ssh_auth_type="password", ssh_password="x", agent_port=9000)


def _fake_ssh(monkeypatch, out: str = "", err: str = "", rc: int = 0):
    """把 nodes.ssh_client 的 connect/exec 换成假实现，返回最近一次 exec 的命令。"""
    calls: list[str] = []
    client = SimpleNamespace(close=lambda: None)

    def fake_connect(node, timeout=None):
        return client

    def fake_exec(c, command, timeout=None, input_data=None):
        calls.append(command)
        return out, err, rc

    monkeypatch.setattr(nodes.ssh_client, "connect", fake_connect)
    monkeypatch.setattr(nodes.ssh_client, "exec", fake_exec)
    return calls


# ---------- 镜像列表 ----------


def test_list_images_parses_and_sorts(monkeypatch):
    """docker images json 逐行解析：repo:tag 引用、大小转字节、悬挂镜像以 ID 为引用。"""
    _fake_ssh(monkeypatch, out=DOCKER_IMAGES_OUT)
    db = _FakeDB(_node(1, "a"))
    res = asyncio.run(nodes.node_images(1, db))
    assert sorted(i["ref"] for i in res["images"]) == [
        "anemll/dspark-vllm-gx10:0.3.4",
        "fed654cba321",  # <none>:<none> 悬挂镜像 → 用 ID 作为可删除引用
        "fireworks-models/deepseek:latest",
    ]
    by_ref = {i["ref"]: i for i in res["images"]}
    assert by_ref["anemll/dspark-vllm-gx10:0.3.4"]["size"] == int(1.52 * 10 ** 9)
    assert by_ref["fireworks-models/deepseek:latest"]["size"] == 0
    assert by_ref["fed654cba321"]["repo"] is None and by_ref["fed654cba321"]["tag"] is None
    assert by_ref["fed654cba321"]["id"] == "fed654cba321"


def test_list_images_empty(monkeypatch):
    """节点上无镜像（docker 正常但没输出）：返回空列表而非报错。"""
    _fake_ssh(monkeypatch, out="")
    db = _FakeDB(_node(1, "a"))
    res = asyncio.run(nodes.node_images(1, db))
    assert res["images"] == []


def test_list_images_ssh_failure_502(monkeypatch):
    """SSH 执行失败（docker 不可用/权限不足）：502 node_ssh_failed。"""
    _fake_ssh(monkeypatch, out="", err="permission denied", rc=1)
    db = _FakeDB(_node(1, "a"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.node_images(1, db))
    assert ei.value.status_code == 502
    assert ei.value.detail["code"] == Code.NODE_SSH_FAILED


def test_list_images_node_missing_404():
    """节点不存在：404 node_not_found。"""
    db = _FakeDB()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.node_images(9, db))
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == Code.NODE_NOT_FOUND


# ---------- 镜像删除 ----------


def test_delete_image_ok(monkeypatch):
    """docker rmi 成功：返回 ok，命令原样引用镜像名。"""
    calls = _fake_ssh(monkeypatch, out="Untagged: anemll/dspark-vllm-gx10:0.3.4", rc=0)
    db = _FakeDB(_node(1, "a"))
    res = asyncio.run(nodes.node_image_delete(1, "anemll/dspark-vllm-gx10:0.3.4", db))
    assert res == {"ok": True, "image": "anemll/dspark-vllm-gx10:0.3.4",
                   "output": "Untagged: anemll/dspark-vllm-gx10:0.3.4"}
    assert calls == ["docker rmi 'anemll/dspark-vllm-gx10:0.3.4' 2>&1"]


def test_delete_image_in_use_409(monkeypatch):
    """docker rmi 被运行中容器拒绝：409 image_delete_failed，原样透传 docker 输出。"""
    _fake_ssh(monkeypatch, out="conflict: unable to remove repository reference "
                               "anemll/xxx (must force)", rc=1)
    db = _FakeDB(_node(1, "a"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.node_image_delete(1, "anemll/xxx:1.0", db))
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == Code.IMAGE_DELETE_FAILED
    assert "unable to remove" in ei.value.detail["details"]


def test_delete_image_invalid_ref_400():
    """非法镜像引用（含 shell 特殊字符）：400，不会拼进 SSH 命令。"""
    db = _FakeDB(_node(1, "a"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.node_image_delete(1, "img; rm -rf /", db))
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == Code.INVALID_IMAGE_REF


def test_delete_image_ssh_failure_502(monkeypatch):
    """SSH 连接/执行异常：502 node_ssh_failed。"""

    def fake_connect(node, timeout=None):
        raise RuntimeError("ssh connect timeout")

    monkeypatch.setattr(nodes.ssh_client, "connect", fake_connect)
    db = _FakeDB(_node(1, "a"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.node_image_delete(1, "anemll/dspark-vllm-gx10:0.3.4", db))
    assert ei.value.status_code == 502
    assert ei.value.detail["code"] == Code.NODE_SSH_FAILED


def test_delete_image_node_missing_404():
    """节点不存在：404。"""
    db = _FakeDB()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(nodes.node_image_delete(9, "anemll/x:1", db))
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == Code.NODE_NOT_FOUND
