"""Agent 侧回归：任务表 TTL 清理、model_pull Range 断点续传、空 digest 校验、日志流切分。"""

import asyncio
import hashlib
import http.server
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import main as agent_main  # noqa: E402

# Agent 鉴权后测试需携带共享 token（见 test_agent_auth.py 的专门用例）
AUTH = {"Authorization": "Bearer agent-test-token"}


@pytest.fixture(autouse=True)
def _agent_token(monkeypatch):
    monkeypatch.setattr(agent_main, "AGENT_TOKEN", "agent-test-token")


def test_prune_jobs():
    """已完成/失败超过 1h 的任务记录被清理，进行中与新完成保留。"""
    jobs = {
        "a": {"status": "completed", "started": time.time() - 7200},
        "b": {"status": "running", "started": time.time() - 7200},
        "c": {"status": "completed", "started": time.time()},
        "d": {"status": "failed", "started": time.time() - 7200},
    }
    agent_main._prune_jobs(jobs)
    assert "a" not in jobs and "d" not in jobs
    assert "b" in jobs and "c" in jobs


def test_log_segments_newline_and_crlf():
    """\n 与 \r\n 都是完整行（append），不触发原地刷新。"""
    segs, buf, prev = agent_main._log_segments(b"hello\nworld\r\n", b"", False)
    assert segs == [(b"hello", False), (b"world", False)]
    assert buf == b"" and prev is False


def test_log_segments_progress_update_chain():
    """进度条末段覆盖当前行，但换行后必须结束刷新状态。"""
    segs, buf, prev = agent_main._log_segments(b"\r10%\r20%\r100%\n", b"", False)
    assert segs == [(b"10%", True), (b"20%", True), (b"100%", True)]
    assert buf == b"" and prev is False


def test_log_segments_append_normally_after_progress_line():
    """进度行结束后的普通日志必须逐行增长，不能继续替换末行。"""
    segs, buf, prev = agent_main._log_segments(
        b"\r10%\r100%\nstarted\nready\n", b"", False
    )
    assert segs == [
        (b"10%", True),
        (b"100%", True),
        (b"started", False),
        (b"ready", False),
    ]
    assert buf == b"" and prev is False


def test_log_segments_crlf_finishes_progress_then_appends():
    """CRLF 收尾也只覆盖最终进度，下一条普通日志恢复追加。"""
    segs, buf, prev = agent_main._log_segments(
        b"\r10%\r100%\r\nstarted\r\n", b"", False
    )
    assert segs == [
        (b"10%", True),
        (b"100%", True),
        (b"started", False),
    ]
    assert buf == b"" and prev is False


def test_log_segments_partial_buffering():
    """分片到达：未闭合段留缓冲，\r 到达后按刷新行处理，\n 收尾段仍覆盖。"""
    segs, buf, prev = agent_main._log_segments(b"Loading\r12%", b"", False)
    assert segs == [(b"Loading", True)]
    assert buf == b"12%"
    segs2, buf2, prev2 = agent_main._log_segments(b"\r34%\n", buf, prev)
    assert segs2 == [(b"12%", True), (b"34%", True)]
    assert buf2 == b"" and prev2 is False


def test_log_segments_empty_skipped():
    """连续分隔符的空段不输出，换行仍会结束 update 状态。"""
    segs, buf, prev = agent_main._log_segments(b"a\n\r\r\n\n", b"", True)
    assert segs == [(b"a", True)]  # prev=True -> \n 段继承为 update
    assert buf == b"" and prev is False


def test_resolve_pull_url_relative_from_client_ip():
    """相对路径 -> 用下发请求来源 IP 补全控制端地址（http://<ip>:8000）。"""
    assert agent_main._resolve_pull_url(
        "192.168.198.5", "/api/models/files/x?relpath=a&token=t"
    ) == "http://192.168.198.5:8000/api/models/files/x?relpath=a&token=t"


def test_resolve_pull_url_adds_leading_slash():
    assert agent_main._resolve_pull_url(
        "10.0.1.5", "api/images/archive/1?token=t"
    ) == "http://10.0.1.5:8000/api/images/archive/1?token=t"


def test_resolve_pull_url_rejects_absolute_urls():
    """Agent 只接受当前协议下发的相对回拉路径。"""
    for url in (
        "http://192.168.198.5:8000/api/x",
        "http://127.0.0.1:9000/api/x",
        "https://example.com/f",
    ):
        with pytest.raises(Exception):
            agent_main._resolve_pull_url("192.168.198.5", url)


def test_resolve_pull_url_no_client_ip_rejected():
    """缺少来源 IP 时明确 400（而非透传后由 urllib 以未知协议方式报错）。"""
    with pytest.raises(Exception):
        agent_main._resolve_pull_url("", "/api/x")


@pytest.mark.anyio
async def test_log_stream_reads_partial_chunks_promptly():
    """日志流必须实时：read1 读取管道时，第一行应在进程退出前立刻到达。

    回归防护：误用 read(65536) 会阻塞等满缓冲区（或 EOF）才返回，
    长驻容器日志将长期卡在管道里不推送（实时停更）。
    """
    proc = subprocess.Popen(
        ["python3", "-c",
         "import sys,time; print('line-A', flush=True); time.sleep(1.2); print('line-B', flush=True)"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    t0 = time.monotonic()
    try:
        chunk = await asyncio.to_thread(proc.stdout.read1, 1 << 16)
        dt = time.monotonic() - t0
        assert b"line-A" in chunk, chunk
        assert dt < 1.0, f"第一行未实时到达（阻塞在管道缓冲）: {dt:.2f}s"
    finally:
        proc.kill()
        proc.wait()


def test_image_pull_empty_digest_rejected():
    """空 digest 直接 400（消灭 .tar 文件名碰撞与恒失败校验）。"""
    from fastapi.testclient import TestClient

    client = TestClient(agent_main.app)
    r = client.post("/api/image/pull", json={"image": "x:1", "digest": "", "url": "http://x"},
                    headers=AUTH)
    assert r.status_code == 400


def test_image_share_uses_short_lived_scoped_token(monkeypatch, tmp_path):
    """节点长期 token 只用于签发；归档流必须使用绑定 digest 的短期令牌。"""
    import hashlib

    from fastapi.testclient import TestClient

    content = b"verified-image-archive"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(agent_main, "IMAGE_DIR", tmp_path)
    (tmp_path / f"{digest}.tar").write_bytes(content)
    client = TestClient(agent_main.app)

    issued = client.post(
        "/api/image/share", json={"digest": digest}, headers=AUTH,
    )
    assert issued.status_code == 200, issued.text
    share = issued.json()
    assert client.get(share["path"]).status_code == 401
    wrong = client.get(share["path"], headers={"X-Transfer-Token": "wrong"})
    assert wrong.status_code == 401
    streamed = client.get(
        share["path"], headers={"X-Transfer-Token": share["token"]},
    )
    assert streamed.status_code == 200
    assert streamed.content == content


def test_image_fetch_restricts_source_and_reports_transfer_id(monkeypatch, tmp_path):
    """worker 只接受私网 head 共享路径，并把任务 ID 作为进度关联键。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "IMAGE_DIR", tmp_path)
    seen = {}

    def fake_download(*args):
        seen["args"] = args
        return {"ok": True, "bytes": 123}

    monkeypatch.setattr(agent_main, "_download_image_archive", fake_download)
    client = TestClient(agent_main.app)
    payload = {
        "source_url": f"http://10.20.0.1:9000/api/image/share/{'sha256:' + 'a' * 64}",
        "source_token": "short-token",
        "image": "example/image:1",
        "digest": "sha256:" + "a" * 64,
        "size": 123,
        "transfer_id": 42,
    }
    result = client.post("/api/image/fetch", json=payload, headers=AUTH)
    assert result.status_code == 200, result.text
    assert seen["args"][-2:] == ("image-sync", "42")

    payload["source_url"] = "https://example.com/api/image/share/sha256:abc"
    assert client.post("/api/image/fetch", json=payload, headers=AUTH).status_code == 400


def test_compose_validation_rejects_insecure_project_and_env(monkeypatch, tmp_path):
    """compose 输入校验：非法 project（含路径段/越权串）与非法 env key 直接 400。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "WORK_DIR", tmp_path)
    client = TestClient(agent_main.app)
    # 路径段 / 越出工作目录的 project 名
    for bad in ("../evil", "..", ".", "..foo"):
        r = client.post("/api/compose/up", json={
            "project": bad, "compose_yaml": "services: {}", "env": {},
        }, headers=AUTH)
        assert r.status_code == 400, (bad, r.text)
        r = client.post("/api/compose/down", json={"project": bad}, headers=AUTH)
        assert r.status_code == 400, (bad, r.text)
    # 非法 env key（含空格/换行）
    project_dir = tmp_path / "smoke-proj"
    project_dir.mkdir()
    compose_file = project_dir / "compose.yml"
    compose_file.write_text("services:\n  existing: {}\n", encoding="utf-8")
    r = client.post("/api/compose/up", json={
        "project": "smoke-proj", "compose_yaml": "services:\n  replacement: {}\n",
        "env": {"BAD KEY": "v"},
    }, headers=AUTH)
    assert r.status_code == 400, r.text
    assert compose_file.read_text(encoding="utf-8") == "services:\n  existing: {}\n"


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """支持 Range 的最小 HTTP 文件服务（模拟控制平面 FileResponse）。"""

    data = b""
    last_range: str | None = None
    requests = 0

    def do_GET(self):
        _RangeHandler.requests += 1
        total = len(self.data)
        rng = self.headers.get("Range")
        _RangeHandler.last_range = rng
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            body = self.data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        else:
            body = self.data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_model_pull_resumes_from_part(monkeypatch, tmp_path):
    """中断保留 .part 后，重试带 Range 从断点续传并完整落盘。"""
    from fastapi.testclient import TestClient

    content = bytes(range(256)) * 4000  # 1MB
    _RangeHandler.data = content
    _RangeHandler.last_range = None
    _RangeHandler.requests = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        hf = tmp_path / "hf"
        monkeypatch.setattr(agent_main, "DEFAULT_HF_CACHE", hf)
        # 模拟中断：.part 已有前 500KB
        rel = "weights/model-00001.safetensors"
        part = hf / "hub" / "models--owner--repo" / (rel + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(content[: 500 * 1024])

        client = TestClient(agent_main.app)
        monkeypatch.setattr(
            agent_main, "_resolve_pull_url", lambda _client_ip, _url: f"http://127.0.0.1:{port}/f",
        )
        digest = hashlib.sha256(content).hexdigest()
        r = client.post("/api/model/pull", json={
            "repo": "owner/repo", "relpath": rel,
            "url": "/api/models/files/owner/repo", "size": len(content),
            "hash_algo": "sha256", "digest": digest, "transfer_id": 1,
        }, headers=AUTH)
        assert r.status_code == 200, r.text
        assert _RangeHandler.last_range == f"bytes={500 * 1024}-"  # 携带续传头
        target = hf / "hub" / "models--owner--repo" / rel
        assert target.read_bytes() == content  # 续传后内容完整
        assert not part.exists()  # 已 rename 收尾
    finally:
        server.shutdown()
        thread.join()


def test_model_share_manifest_and_scoped_file_access(monkeypatch, tmp_path):
    """模型共享保留 HF 布局，并且文件流只能用绑定 share 的短期令牌读取。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "DEFAULT_HF_CACHE", tmp_path)
    root = tmp_path / "hub" / "models--owner--repo"
    content = b"model-weights"
    digest = hashlib.sha256(content).hexdigest()
    blob = root / "blobs" / digest
    blob.parent.mkdir(parents=True)
    blob.write_bytes(content)
    (root / "refs").mkdir()
    (root / "refs" / "main").write_text("commit")
    snapshot = root / "snapshots" / "commit"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").symlink_to(f"../../blobs/{digest}")

    client = TestClient(agent_main.app)
    issued = client.post("/api/model/share", json={"repo": "owner/repo"}, headers=AUTH)
    assert issued.status_code == 200, issued.text
    share = issued.json()
    assert share["total_size"] == len(content) + len("commit")
    assert any(e["type"] == "symlink" for e in share["manifest"])
    assert client.get(share["path"], params={"relpath": f"blobs/{digest}"}).status_code == 401
    streamed = client.get(
        share["path"], params={"relpath": f"blobs/{digest}"},
        headers={"X-Transfer-Token": share["token"]},
    )
    assert streamed.status_code == 200 and streamed.content == content


def test_same_size_corrupt_model_file_is_not_reused(tmp_path):
    target = tmp_path / "blob"
    target.write_bytes(b"bad!")
    digest = hashlib.sha256(b"good").hexdigest()
    assert not agent_main._model_file_matches(target, 4, "sha256", digest)


def test_model_pull_requires_current_integrity_protocol():
    from fastapi.testclient import TestClient

    client = TestClient(agent_main.app)
    payload = {
        "repo": "owner/repo", "relpath": "blobs/a", "url": "/api/models/files/x",
        "size": 4, "transfer_id": 1,
    }
    response = client.post("/api/model/pull", json=payload, headers=AUTH)
    assert response.status_code == 400
    assert "内容摘要" in response.json()["detail"]


def test_model_fetch_rejects_public_or_untrusted_source():
    from fastapi.testclient import TestClient

    client = TestClient(agent_main.app)
    payload = {
        "source_url": "https://example.com/api/model/share/x",
        "source_token": "short-token", "repo": "owner/repo",
        "manifest": [], "total_size": 0, "transfer_id": 1,
    }
    assert client.post("/api/model/fetch", json=payload, headers=AUTH).status_code == 400


def test_completed_model_fetch_ttl_starts_at_finish(monkeypatch):
    """长任务刚完成时仍应保留一个完整 TTL，供后端重启后接管。"""
    monkeypatch.setattr(agent_main.time, "time", lambda: 10_000)
    jobs = {
        "recent": {"status": "completed", "started": 1, "finished": 9_999},
        "stale": {"status": "failed", "started": 1, "finished": 6_000},
    }
    assert agent_main._prune_jobs(jobs, ttl=3600) == ["stale"]
    assert "recent" in jobs


# ---------- Phase3：推理服务基准聚合 ----------


def test_aggregate_benchmark_ok():
    """并发压测聚合：tok/s、TTFT/E2E/ITL 分位、成功/失败计数。"""
    def mk(ttft, e2e, tokens, t_start, t_end):
        return {"ok": True, "ttft": ttft, "e2e": e2e, "tokens": tokens,
                "itl_p50": 0.05, "itl_p95": 0.08,
                "t_start": t_start, "t_end": t_end}

    results = [
        mk(0.05, 0.5, 32, 0.0, 0.5),
        mk(0.1, 0.6, 64, 0.0, 0.6),
        {"ok": False, "error": "boom"},
    ]
    out = agent_main._aggregate_benchmark("http://127.0.0.1:8888", results, 8, 3)
    assert out["ok"] and out["succeeded"] == 2 and out["failed"] == 1
    assert out["total_tokens"] == 96
    # span = 0.6 - 0.0 = 0.6s => 96/0.6 = 160 tok/s
    assert out["tokens_per_sec"] == 160.0
    # ttfts sorted [0.05, 0.1]：p50 取上分位 0.1 -> 100ms
    assert out["ttft_p50_ms"] == 100.0
    assert out["e2e_p50_ms"] == 600.0
    assert out["itl_p50_ms"] == 50.0
    assert len(out["per_request"]) == 2


def test_aggregate_benchmark_all_failed():
    out = agent_main._aggregate_benchmark(
        "http://127.0.0.1:8888", [{"ok": False, "error": "connect"}], 4, 1
    )
    assert out["ok"] is False and out["failed"] == 1 and out.get("error") == "connect"
