"""B2：流式哈希与整块哈希结果一致（回归：read_bytes OOM 修复）。"""

import hashlib

from app.services.model_manager import _file_sha256, _git_blob_sha1


def test_file_sha256_matches_read_bytes(tmp_path):
    content = b"hello world\n" * 1000
    path = tmp_path / "blob"
    path.write_bytes(content)
    assert _file_sha256(path) == hashlib.sha256(content).hexdigest()


def test_git_blob_sha1_matches_bytes_version(tmp_path):
    # 与旧实现 _git_blob_sha1(bytes) 的结果保持一致
    content = bytes(range(256)) * 4
    path = tmp_path / "blob"
    path.write_bytes(content)
    expected = hashlib.sha1(
        b"blob %d\0" % len(content) + content, usedforsecurity=False
    ).hexdigest()
    assert _git_blob_sha1(path) == expected


def test_hashes_stable_across_calls(tmp_path):
    content = b"dgx-spark-model-shard" * 5000
    path = tmp_path / "blob"
    path.write_bytes(content)
    assert _file_sha256(path) == _file_sha256(path)
    assert _git_blob_sha1(path) == _git_blob_sha1(path)
