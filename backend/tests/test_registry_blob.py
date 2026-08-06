"""B1：_registry_blob_file 416 分支（Range 越界 = .part 已完成）收尾逻辑回归。

修复前：416 直接 break，.part 永不 rename 成 dest，后续每次都重复撞 416 卡死。
修复后：416 时校验已下载内容，通过则 rename 落盘；损坏/超长则删除重下。
"""

import hashlib

from app.services.image_manager import _registry_blob_file


class _Resp:
    """极简 httpx 响应替身（仅覆盖 _registry_blob_file 用到的接口）。"""

    def __init__(self, status_code: int, data: bytes = b""):
        self.status_code = status_code
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, chunk: int):
        for i in range(0, len(self._data), chunk):
            yield self._data[i : i + chunk]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"fake status {self.status_code}")


class _FakeClient:
    """模拟"有 .part 时服务器对 Range 返回 416，无 Range 时返回完整内容"的行为。"""

    def __init__(self, full_data: bytes):
        self.full_data = full_data
        self.range_calls = 0

    def stream(self, method, url, headers=None, follow_redirects=True, timeout=None):
        if (headers or {}).get("Range"):
            self.range_calls += 1
            return _Resp(416)  # Range 越界
        return _Resp(200, data=self.full_data)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_416_with_complete_part_finalizes_dest(tmp_path):
    data = b"complete-layer-bytes" * 100
    dest = tmp_path / "blob"
    part = tmp_path / "blob.part"
    part.write_bytes(data)  # .part 已完整
    _registry_blob_file(_FakeClient(data), "host", "path", _digest(data), "", dest,
                        expect_size=len(data))
    assert dest.exists() and dest.read_bytes() == data
    assert not part.exists()  # 已 rename 收尾


def test_416_with_corrupt_part_redownloads(tmp_path):
    data = b"correct-layer-bytes" * 100
    dest = tmp_path / "blob"
    part = tmp_path / "blob.part"
    part.write_bytes(b"corrupted-partial-content")  # 内容与 digest 不符
    client = _FakeClient(data)
    _registry_blob_file(client, "host", "path", _digest(data), "", dest,
                        expect_size=len(data))
    assert dest.exists() and dest.read_bytes() == data  # 已重下并落盘
    assert client.range_calls >= 1  # 至少经历了一次 416 -> 重下
