"""镜像高速传输编排回归：权威网络 IP、短期令牌和 Agent 直拉。"""

import gzip
import io
import json
import tarfile

import pytest
import zstandard
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Cluster, ClusterNode, Node
from app.services import image_manager, peer_transfer


def test_node_transfer_ip_prefers_cluster_plan():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    node = Node(
        id=1, name="head", ip="192.0.2.10",
        hardware_info={"roce": [{"rocev2_ip": "10.99.0.9"}]},
    )
    cluster = Cluster(
        id=1, name="c1", network_type="roce",
        network_plan={
            "iface_subnets": {"enp1s0f0np0": "10.20.0.0/24"},
            "cidr": "10.20.0.0/24", "mtu": 9000,
        },
    )
    db.add_all([node, cluster])
    db.add(ClusterNode(cluster_id=1, node_id=1, net_index=3))
    db.commit()

    assert peer_transfer.node_transfer_ip(db, node) == "10.20.0.12"
    db.close()


@pytest.mark.anyio
async def test_worker_fetch_uses_head_share_token(monkeypatch):
    worker = Node(id=2, name="worker", ip="192.0.2.11", agent_port=9000)
    seen = {}

    async def fake_fetch(node, payload):
        seen.update(node=node, payload=payload)
        return {"ok": True, "bytes": 500}

    monkeypatch.setattr(image_manager.agent_client, "image_fetch", fake_fetch)
    node_id, result = await image_manager._sync_archive_to_worker(
        worker, 42, "example/image:1", "sha256:abc", 500,
        "http://10.20.0.1:9000/api/image/share/sha256:abc", "short-token",
    )

    assert node_id == worker.id and result["status"] == "completed"
    assert seen["node"] is worker
    assert seen["payload"]["source_url"] == (
        "http://10.20.0.1:9000/api/image/share/sha256:abc"
    )
    assert seen["payload"]["source_token"] == "short-token"
    assert seen["payload"]["transfer_id"] == 42


@pytest.mark.anyio
async def test_current_agent_capability_is_accepted(monkeypatch):
    node = Node(id=1, name="n1", ip="192.0.2.1")

    async def fake_info(_node):
        return {"capabilities": ["image_peer_transfer_v1"]}

    monkeypatch.setattr(image_manager.agent_client, "info", fake_info)
    assert await peer_transfer.check_agent_capability(
        node, image_manager.agent_client, "image_peer_transfer_v1",
    ) is None


@pytest.mark.anyio
async def test_missing_agent_capability_fails_without_mutation(monkeypatch):
    node = Node(id=1, name="outdated", ip="192.0.2.1")

    async def fake_info(_node):
        return {"agent_version": "0.1.0"}

    monkeypatch.setattr(image_manager.agent_client, "info", fake_info)
    error = await peer_transfer.check_agent_capability(
        node, image_manager.agent_client, "image_peer_transfer_v1",
    )
    assert "重新部署 Agent" in error


def _make_plain_layer_tar() -> bytes:
    """构造一个真实 layer（plain tar，含一个文件）。"""
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "hello.txt").write_text("hello")
        return subprocess.run(
            ["tar", "-cf", "-", "-C", d, "hello.txt"],
            capture_output=True, check=True,
        ).stdout


def _extract_layer_tar(archive: str) -> bytes:
    """模拟 docker load：读 manifest.json 后解出第一个 layer 的原始字节。"""
    with tarfile.open(archive) as tf:
        manifest = json.loads(tf.extractfile("manifest.json").read())
        assert manifest[0]["Layers"]
        return tf.extractfile(manifest[0]["Layers"][0]).read()


def test_build_archive_zstd_layer_decompressed(tmp_path):
    """zstd 压缩层必须解压为 plain tar，否则 docker load 报 invalid tar header。"""
    plain = _make_plain_layer_tar()
    blob = tmp_path / "layer.blob"
    blob.write_bytes(zstandard.ZstdCompressor().compress(plain))
    dest = tmp_path / "image.tar"
    manifest = {
        "config": {"digest": "sha256:" + "c" * 64},
        "layers": [{"digest": "sha256:" + "d" * 64}],
    }
    image_manager._build_docker_archive(
        "example/app:1", manifest, b"{}",
        [("sha256:" + "d" * 64, blob)], dest,
    )
    # 解出的 layer 必须是有效 plain tar 且内容与原始一致（docker load 可解）
    layer = _extract_layer_tar(str(dest))
    with tarfile.open(fileobj=io.BytesIO(layer)) as tf:
        assert b"hello" in tf.extractfile("hello.txt").read()


def test_build_archive_gzip_and_plain_layers(tmp_path):
    """回归：gzip 层解压、plain 层原样保留的行为不变。"""
    plain = _make_plain_layer_tar()
    gz_blob = tmp_path / "layer-gz.blob"
    gz_blob.write_bytes(gzip.compress(plain))
    plain_blob = tmp_path / "layer-plain.blob"
    plain_blob.write_bytes(plain)
    dest = tmp_path / "image.tar"
    manifest = {
        "config": {"digest": "sha256:" + "c" * 64},
        "layers": [
            {"digest": "sha256:" + "1" * 64},
            {"digest": "sha256:" + "2" * 64},
        ],
    }
    image_manager._build_docker_archive(
        "example/app:1", manifest, b"{}",
        [("sha256:" + "1" * 64, gz_blob), ("sha256:" + "2" * 64, plain_blob)],
        dest,
    )
    with tarfile.open(dest) as tf:
        manifest_out = json.loads(tf.extractfile("manifest.json").read())
        layers = manifest_out[0]["Layers"]
        assert len(layers) == 2
        for name in layers:
            content = tf.extractfile(name).read()
            with tarfile.open(fileobj=io.BytesIO(content)) as lt:
                assert b"hello" in lt.extractfile("hello.txt").read()
