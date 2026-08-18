"""镜像高速传输编排回归：权威网络 IP、短期令牌和 Agent 直拉。"""

import gzip
import hashlib
import io
import json
import tarfile
import threading

import pytest
import zstandard
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Cluster, ClusterNode, ImageTransfer, Node
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


def _cached_transfer_session(monkeypatch, tmp_path, job_id):
    """文件库 + 把 SessionLocal/缓存目录指向测试环境，返回 (sessionmaker, 归档路径)。

    anyio（异步线程）下必须用文件库：内存 sqlite 每连接一个独立库，跨线程看不到表。
    """
    engine = create_engine(f"sqlite:///{tmp_path}/fw.db")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    monkeypatch.setattr(image_manager, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(image_manager, "SessionLocal", S)
    t = ImageTransfer(id=job_id, image="example/app:1", digest="",
                      head_node_id=None, status="completed", size_bytes=0,
                      sync_jobs={})
    db = S()
    db.add(t)
    db.commit()
    db.close()
    return S, image_manager.image_archive_path("example/app:1", "")


def test_start_pull_cached_archive_reuses_sidecar_digest(monkeypatch, tmp_path):
    """缓存归档 + 有效 sidecar：直接复用指纹，不再全量重算（重复分发性能回归）。"""
    S, dest = _cached_transfer_session(monkeypatch, tmp_path, 901)
    dest.write_bytes(b"cached-archive-bytes")
    known = "sha256:" + hashlib.sha256(dest.read_bytes()).hexdigest()
    image_manager._mark_archive_digest(dest, known)

    recomputed = []
    monkeypatch.setattr(
        image_manager, "_archive_fingerprint",
        lambda p: recomputed.append(p) or "wrong",
    )
    image_manager._start_pull(901)

    db = S()
    t = db.get(ImageTransfer, 901)
    assert t.digest == known
    assert t.size_bytes == dest.stat().st_size
    assert recomputed == []  # 未触发整份归档重读
    db.close()


def test_start_pull_parallel_progress_commit_is_thread_safe(monkeypatch, tmp_path):
    """并行层下载从多线程回调进度，同一 Session 的并发 commit 必须串行化。

    回归：并行拉取后进度回调经 ThreadPoolExecutor 从多个工作线程调用
    on_progress/on_phase，无锁时并发 commit 抛 InvalidRequestError
    （"Method 'commit()' can't be called here..."）使拉取随机失败、任务卡死。
    """
    S, dest = _cached_transfer_session(monkeypatch, tmp_path, 904)
    db = S()
    db.get(ImageTransfer, 904).status = "pulling"
    db.commit()
    db.close()

    total = 10 * 1024 * 1024
    errors: list[Exception] = []

    def fake_pull_image(_image, _dest, progress=None, phase=None):
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                for _ in range(80):
                    if progress:
                        progress(total, total)  # done==total，跳过节流，每次必写库
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if errors:
            raise errors[0]  # 与真实链路一致：工作线程异常经 future.result() 上行
        if phase:
            phase("packing")

    monkeypatch.setattr(image_manager, "pull_image", fake_pull_image)
    image_manager._start_pull(904)

    assert errors == []  # 并发 commit 未触发 SQLAlchemy 状态竞争
    db = S()
    t = db.get(ImageTransfer, 904)
    assert t.status == "packing"
    assert t.error is None
    db.close()


def test_registry_blob_file_cached_skip_uses_marker(monkeypatch, tmp_path):
    """blob 已存在且 sidecar 命中：跳过下载，不再发起请求也不再整份重读。"""
    from types import SimpleNamespace

    content = b"verified-blob-bytes"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    dest = tmp_path / "blob"
    dest.write_bytes(content)
    image_manager._mark_archive_digest(dest, digest)

    def boom(*a, **k):
        raise AssertionError("缓存命中时不应发起请求或重读文件")

    client = SimpleNamespace(stream=boom)
    monkeypatch.setattr(image_manager, "_archive_fingerprint", boom)
    image_manager._registry_blob_file(
        client, "r.example", "library/x", digest, "", dest,
        expect_size=len(content),
    )  # 不抛异常即通过


def test_registry_blob_file_cached_skip_hashes_once_and_persists_marker(
        monkeypatch, tmp_path
):
    """blob 已存在但无 sidecar（升级自旧库）：重算一次并落标记供后续复用。"""
    from types import SimpleNamespace

    content = b"verified-blob-bytes"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    dest = tmp_path / "blob"
    dest.write_bytes(content)

    client = SimpleNamespace(stream=lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(image_manager, "_archive_fingerprint",
                        lambda p: digest)
    image_manager._registry_blob_file(
        client, "r.example", "library/x", digest, "", dest,
        expect_size=len(content),
    )
    assert image_manager._cached_archive_digest(dest) == digest  # 标记已落
    # 第二次直接命中标记，_archive_fingerprint 不再被调用
    monkeypatch.setattr(image_manager, "_archive_fingerprint",
                        lambda p: (_ for _ in ()).throw(AssertionError()))
    image_manager._registry_blob_file(
        client, "r.example", "library/x", digest, "", dest,
        expect_size=len(content),
    )


def test_start_pull_cached_archive_computes_and_persists_marker(monkeypatch, tmp_path):
    """缓存归档但无 sidecar（升级自旧库）：重算一次并落标记供后续复用。"""
    S, dest = _cached_transfer_session(monkeypatch, tmp_path, 902)
    dest.write_bytes(b"cached")
    known = "sha256:" + hashlib.sha256(b"cached").hexdigest()
    real_fp = image_manager._archive_fingerprint
    recomputes = []
    monkeypatch.setattr(
        image_manager, "_archive_fingerprint",
        lambda p: recomputes.append(p) or real_fp(p),
    )

    image_manager._start_pull(902)
    assert recomputes == [dest]  # 无 sidecar 时只重算一次
    db = S()
    t = db.get(ImageTransfer, 902)
    assert t.digest == known
    assert t.size_bytes == dest.stat().st_size
    assert image_manager._cached_archive_digest(dest) == known  # 标记已落

    # 同一缓存再次分发：直接复用，不再重算
    recomputes.clear()
    image_manager._start_pull(902)
    assert recomputes == []
    db.close()


@pytest.mark.anyio
async def test_start_image_transfer_uses_cached_archive_when_registry_down(
        monkeypatch, tmp_path
):
    """registry 不可达但归档已缓存：仍可创建分发任务（离线/受限网络）。"""
    S, dest = _cached_transfer_session(monkeypatch, tmp_path, 903)
    dest.write_bytes(b"cached")

    def boom(_image):
        raise RuntimeError("registry 不可达")

    monkeypatch.setattr(image_manager, "inspect_image", boom)
    monkeypatch.setattr(image_manager, "_start_pull", lambda *a, **k: None)
    monkeypatch.setattr(image_manager, "spawn", lambda coro: coro.close())

    t = await image_manager.start_image_transfer("example/app:1", 1, [2], False)
    assert t.status == "pulling"
    assert t.digest == ""  # 归档指纹统一由 _start_pull 计算
    assert t.size_bytes == len(b"cached")


@pytest.mark.anyio
async def test_start_image_transfer_registry_down_without_cache_fails(
        monkeypatch, tmp_path
):
    """registry 不可达且无缓存归档：按原行为上报检查失败。"""
    engine = create_engine(f"sqlite:///{tmp_path}/fw.db")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(image_manager, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(image_manager, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(
        image_manager, "inspect_image",
        lambda _image: (_ for _ in ()).throw(RuntimeError("registry 不可达")),
    )
    with pytest.raises(RuntimeError):
        await image_manager.start_image_transfer("example/app:1", None, [], False)


def test_pull_via_registry_downloads_layers_in_parallel_in_manifest_order(
        monkeypatch, tmp_path
):
    """并行拉取编排：层并发下载、按 manifest 顺序组装、进度累计到 total。"""
    import threading
    import time

    monkeypatch.setattr(image_manager, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(image_manager, "_parse_image",
                        lambda im: ("r.example", "library/x", "1"))
    monkeypatch.setattr(image_manager, "_registry_token", lambda *a, **k: "")
    manifest = {
        "config": {"digest": "sha256:" + "c" * 64},
        "layers": [
            {"digest": "sha256:" + "a1" * 32, "size": 11},
            {"digest": "sha256:" + "b2" * 32, "size": 22},
        ],
    }
    monkeypatch.setattr(image_manager, "_registry_manifest",
                        lambda *a, **k: (manifest, "sha256:d"))
    monkeypatch.setattr(image_manager, "_registry_blob",
                        lambda *a, **k: b'{"architecture":"arm64","os":"linux"}')

    counter = {"active": 0, "max": 0}
    lock = threading.Lock()

    def fake_blob_file(client, host, path, ld, token, lp, expect_size=None,
                       progress=None):
        with lock:
            counter["active"] += 1
            counter["max"] = max(counter["max"], counter["active"])
        time.sleep(0.05)  # 放大并发窗口
        lp.write_bytes(b"x" * (expect_size or 1))
        with lock:
            counter["active"] -= 1

    monkeypatch.setattr(image_manager, "_registry_blob_file", fake_blob_file)

    assembled = {}
    progress = []

    def fake_build(_image, _manifest, _cfg, layer_files, _dest):
        assembled["files"] = [ld for ld, _ in layer_files]

    monkeypatch.setattr(image_manager, "_build_docker_archive", fake_build)

    dest = tmp_path / "out.tar"
    digest = image_manager._pull_via_registry(
        "x:1", dest, None,
        progress=lambda d, t: progress.append((d, t)),
    )
    assert digest == "sha256:d"
    assert counter["max"] == 2  # 两层真正并行下载
    # manifest 顺序组装（docker-archive Layers 顺序必须与 manifest 一致）
    assert assembled["files"] == [m["digest"] for m in manifest["layers"]]
    # 进度最终累计到总字节
    assert progress and progress[-1] == (33, 33)


def test_build_archive_is_deterministic_across_runs(tmp_path):
    """并行解压 + 固定顺序写 tar：同一镜像两次组装字节一致（传输 digest 稳定）。"""
    plain = _make_plain_layer_tar()
    blobs = []
    for k in range(3):
        b = tmp_path / f"l{k}.blob"
        b.write_bytes(zstandard.ZstdCompressor().compress(plain))
        blobs.append(b)
    digests = [f"sha256:{str(i) * 64}" for i in (7, 8, 9)]
    manifest = {
        "config": {"digest": "sha256:" + "c" * 64},
        "layers": [{"digest": d} for d in digests],
    }
    outs = []
    for n in range(2):
        dest = tmp_path / f"image{n}.tar"
        image_manager._build_docker_archive(
            "example/app:1", manifest, b"{}",
            list(zip(digests, blobs)), dest,
        )
        outs.append(hashlib.sha256(dest.read_bytes()).hexdigest())
        assert not list(tmp_path.glob("*.plain"))  # 临时解压文件已清理
    assert outs[0] == outs[1]
