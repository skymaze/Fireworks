"""Agent 侧回归：任务表 TTL 清理、model_pull Range 断点续传、空 digest 校验、日志流切分。"""

import asyncio
import hashlib
import http.server
import json
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
    assert (
        agent_main._resolve_pull_url(
            "192.168.198.5", "/api/models/files/x?relpath=a&token=t"
        )
        == "http://192.168.198.5:8000/api/models/files/x?relpath=a&token=t"
    )


def test_resolve_pull_url_adds_leading_slash():
    assert (
        agent_main._resolve_pull_url("10.0.1.5", "api/images/archive/1?token=t")
        == "http://10.0.1.5:8000/api/images/archive/1?token=t"
    )


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
        [
            "python3",
            "-c",
            "import sys,time; print('line-A', flush=True); time.sleep(1.2); print('line-B', flush=True)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
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
    r = client.post(
        "/api/image/pull",
        json={"image": "x:1", "digest": "", "url": "http://x"},
        headers=AUTH,
    )
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
        "/api/image/share",
        json={"digest": digest},
        headers=AUTH,
    )
    assert issued.status_code == 200, issued.text
    share = issued.json()
    assert client.get(share["path"]).status_code == 401
    wrong = client.get(share["path"], headers={"X-Transfer-Token": "wrong"})
    assert wrong.status_code == 401
    streamed = client.get(
        share["path"],
        headers={"X-Transfer-Token": share["token"]},
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
    assert (
        client.post("/api/image/fetch", json=payload, headers=AUTH).status_code == 400
    )


class _TruncateOnceHandler(http.server.BaseHTTPRequestHandler):
    """首次完整声明 Content-Length 但只发一半即掐断，后续支持 Range 续传。

    模拟管理网/RoCE 上最常见的中途断流（控制平面未传完就断开）。
    """

    data = b""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        total = len(self.data)
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            body = self.data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(total))
        self.end_headers()
        half = total // 2
        self.wfile.write(self.data[:half])
        self.wfile.flush()
        self.close_connection = True  # 中途掐断

    def log_message(self, *a):
        pass


def test_image_archive_pull_resumes_after_truncation(monkeypatch, tmp_path):
    """控制平面中途断流后必须 Range 续传成功，而不是让分发任务直接失败。"""
    content = bytes(range(256)) * 4000  # 1MB
    _TruncateOnceHandler.data = content
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TruncateOnceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setattr(agent_main, "IMAGE_DIR", tmp_path)
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        target = tmp_path / f"{digest}.tar"
        res = agent_main._download_image_archive(
            f"http://127.0.0.1:{port}/a.tar",
            {"Authorization": "Bearer agent-test-token"},
            target, digest, len(content), "image", digest,
        )
        assert res.get("ok") is True
        assert target.read_bytes() == content  # 续传后内容完整
        assert not target.with_name(target.name + ".part").exists()
    finally:
        server.shutdown()
        thread.join()


def test_image_archive_pull_skips_without_rehashing_when_marker_valid(
        monkeypatch, tmp_path
):
    """已存在且完整的归档：再次下发改动直接跳过，不再整份重新校验（快路径）。"""
    content = b"complete-archive-bytes"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(agent_main, "IMAGE_DIR", tmp_path)
    target = tmp_path / f"{digest}.tar"
    target.write_bytes(content)
    agent_main._mark_archive_verified(target, digest)

    # 标记有效时不应重读文件；URL 指向不可达地址，若真去下载会立即失败
    monkeypatch.setattr(
        agent_main, "_file_fingerprint",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应重新校验")),
    )
    res = agent_main._download_image_archive(
        "http://127.0.0.1:1/a.tar",
        {"Authorization": "Bearer agent-test-token"},
        target, digest, len(content), "image", digest,
    )
    assert res.get("skipped") is True


def test_compose_validation_rejects_insecure_project_and_env(monkeypatch, tmp_path):
    """compose 输入校验：非法 project（含路径段/越权串）与非法 env key 直接 400。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "WORK_DIR", tmp_path)
    client = TestClient(agent_main.app)
    # 路径段 / 越出工作目录的 project 名
    for bad in ("../evil", "..", ".", "..foo"):
        r = client.post(
            "/api/compose/up",
            json={
                "project": bad,
                "compose_yaml": "services: {}",
                "env": {},
            },
            headers=AUTH,
        )
        assert r.status_code == 400, (bad, r.text)
        r = client.post("/api/compose/down", json={"project": bad}, headers=AUTH)
        assert r.status_code == 400, (bad, r.text)
    # 非法 env key（含空格/换行）
    project_dir = tmp_path / "smoke-proj"
    project_dir.mkdir()
    compose_file = project_dir / "compose.yml"
    compose_file.write_text("services:\n  existing: {}\n", encoding="utf-8")
    r = client.post(
        "/api/compose/up",
        json={
            "project": "smoke-proj",
            "compose_yaml": "services:\n  replacement: {}\n",
            "env": {"BAD KEY": "v"},
        },
        headers=AUTH,
    )
    assert r.status_code == 400, r.text
    assert compose_file.read_text(encoding="utf-8") == "services:\n  existing: {}\n"



def test_compose_action_validation_and_rc(monkeypatch, tmp_path):
    """compose 生命周期端点：非法 action/project 返回 400；操作失败转 502。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "WORK_DIR", tmp_path)
    client = TestClient(agent_main.app)
    for bad_action in ("kill", "", "STOP", "up"):
        r = client.post(
            "/api/compose/action",
            json={"project": "p", "action": bad_action},
            headers=AUTH,
        )
        assert r.status_code == 400, (bad_action, r.text)
    for bad in ("../evil", "..", "."):
        r = client.post(
            "/api/compose/action",
            json={"project": bad, "action": "stop"},
            headers=AUTH,
        )
        assert r.status_code == 400, (bad, r.text)

    # 合法动作但 docker 失败 -> 502（run_cmd 返回 (out, rc, err)）
    monkeypatch.setattr(agent_main, "run_cmd", lambda *a, **k: ("out", 1, "err"))
    r = client.post(
        "/api/compose/action",
        json={"project": "okproj", "action": "restart"},
        headers=AUTH,
    )
    assert r.status_code == 502
    assert "restart 失败" in r.text



def test_compose_ps_includes_container_health(monkeypatch, tmp_path):
    """compose ps 返回容器 Health（docker inspect 取 State.Health.Status）。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "WORK_DIR", tmp_path)
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "compose.yml").write_text("services: {}\n")

    calls = []

    def fake_run_cmd(cmd, **kw):
        calls.append(list(cmd))
        if "ps" in cmd:
            return ('{"Service":"vllm","Name":"proj-1","State":"running","Status":"Up","Health":"checking"}\n', 0, "")
        if "inspect" in cmd:
            return ("healthy", 0, "")
        return ("", 0, "")

    monkeypatch.setattr(agent_main, "run_cmd", fake_run_cmd)
    client = TestClient(agent_main.app)
    r = client.post("/api/compose/ps", json={"project": "proj"}, headers=AUTH)
    assert r.status_code == 200
    c = r.json()["containers"][0]
    assert c["name"] == "proj-1"
    assert c["health"] == "healthy"
    assert any("inspect" in line for line in calls)  # 每个容器一次 inspect


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
            agent_main,
            "_resolve_pull_url",
            lambda _client_ip, _url: f"http://127.0.0.1:{port}/f",
        )
        digest = hashlib.sha256(content).hexdigest()
        r = client.post(
            "/api/model/pull",
            json={
                "repo": "owner/repo",
                "relpath": rel,
                "url": "/api/models/files/owner/repo",
                "size": len(content),
                "hash_algo": "sha256",
                "digest": digest,
                "transfer_id": 1,
            },
            headers=AUTH,
        )
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
    assert (
        client.get(share["path"], params={"relpath": f"blobs/{digest}"}).status_code
        == 401
    )
    streamed = client.get(
        share["path"],
        params={"relpath": f"blobs/{digest}"},
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
        "repo": "owner/repo",
        "relpath": "blobs/a",
        "url": "/api/models/files/x",
        "size": 4,
        "transfer_id": 1,
    }
    response = client.post("/api/model/pull", json=payload, headers=AUTH)
    assert response.status_code == 400
    assert "内容摘要" in response.json()["detail"]


def test_model_fetch_rejects_public_or_untrusted_source():
    from fastapi.testclient import TestClient

    client = TestClient(agent_main.app)
    payload = {
        "source_url": "https://example.com/api/model/share/x",
        "source_token": "short-token",
        "repo": "owner/repo",
        "manifest": [],
        "total_size": 0,
        "transfer_id": 1,
    }
    assert (
        client.post("/api/model/fetch", json=payload, headers=AUTH).status_code == 400
    )


def test_completed_model_fetch_ttl_starts_at_finish(monkeypatch):
    """长任务刚完成时仍应保留一个完整 TTL，供后端重启后接管。"""
    monkeypatch.setattr(agent_main.time, "time", lambda: 10_000)
    jobs = {
        "recent": {"status": "completed", "started": 1, "finished": 9_999},
        "stale": {"status": "failed", "started": 1, "finished": 6_000},
    }
    assert agent_main._prune_jobs(jobs, ttl=3600) == ["stale"]
    assert "recent" in jobs


# ---------- 推理服务基准聚合 ----------


def test_aggregate_benchmark_ok():
    """并发压测聚合：tok/s、TTFT/E2E/ITL 分位、成功/失败计数。"""

    def mk(ttft, e2e, tokens, t_start, t_end):
        return {
            "ok": True,
            "ttft": ttft,
            "e2e": e2e,
            "tokens": tokens,
            "itl_p50": 0.05,
            "itl_p95": 0.08,
            "t_start": t_start,
            "t_end": t_end,
        }

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


# ---------- 推理统计：原始快照解析与无状态返回 ----------


PRE_BODY = "\n".join(
    [
        "# TYPE vllm:generation_tokens_total counter",
        'vllm:generation_tokens_total{model_name="m",engine="e"} 100',
        'vllm:prompt_tokens_total{model_name="m",engine="e"} 50',
        'vllm:request_success_total{finished_reason="stop"} 3',
        'vllm:request_success_total{finished_reason="abort"} 1',
        'vllm:kv_cache_usage_perc{model_name="m",engine="e"} 0.3',
        'vllm:time_to_first_token_seconds_bucket{le="0.05"} 1',
        'vllm:time_to_first_token_seconds_bucket{le="0.1"} 3',
        'vllm:time_to_first_token_seconds_bucket{le="0.5"} 8',
        'vllm:time_to_first_token_seconds_bucket{le="+Inf"} 10',
        "vllm:time_to_first_token_seconds_sum 1.5",
        "vllm:time_to_first_token_seconds_count 10",
    ]
)

CUR_BODY = "\n".join(
    [
        "# TYPE vllm:generation_tokens_total counter",
        'vllm:generation_tokens_total{model_name="m",engine="e"} 160',
        'vllm:prompt_tokens_total{model_name="m",engine="e"} 80',
        'vllm:request_success_total{finished_reason="stop"} 5',
        'vllm:request_success_total{finished_reason="abort"} 1',
        'vllm:kv_cache_usage_perc{model_name="m",engine="e"} 0.45',
        'vllm:time_to_first_token_seconds_bucket{le="0.05"} 3',
        'vllm:time_to_first_token_seconds_bucket{le="0.1"} 6',
        'vllm:time_to_first_token_seconds_bucket{le="0.5"} 14',
        'vllm:time_to_first_token_seconds_bucket{le="+Inf"} 18',
        "vllm:time_to_first_token_seconds_sum 3.0",
        "vllm:time_to_first_token_seconds_count 18",
    ]
)


def test_collect_metrics_snapshot_parsing():
    """计数器多标签系列求和；KV gauge 0..1 -> %；直方图 _sum/_count/_bucket 归组。"""
    snap = agent_main._collect_metrics_snapshot(PRE_BODY)
    assert snap is not None
    assert snap["counters"]["generation_tokens_total"] == 100
    assert snap["counters"]["prompt_tokens_total"] == 50
    assert snap["counters"]["request_success_total"] == 4  # stop(3) + abort(1)
    assert snap["kv_percent"] == 30.0
    ttft = snap["hist"]["ttft"]
    assert ttft["sum"] == 1.5 and ttft["count"] == 10.0
    assert [b[0] for b in ttft["buckets"]] == [0.05, 0.1, 0.5, float("inf")]
    assert [b[1] for b in ttft["buckets"]] == [1.0, 3.0, 8.0, 10.0]


def test_collect_metrics_snapshot_foreign_metrics_returns_none():
    """非 vLLM /metrics（如 go 运行时指标）解析不到 -> None（诚实空态）。"""
    snap = agent_main._collect_metrics_snapshot(
        "# TYPE go_gc_duration_seconds summary\ngo_gc_duration_seconds 0.5\n"
    )
    assert snap is None


def test_collect_metrics_snapshot_kv_and_tpot_aliases():
    """KV 旧名 gpu_cache_usage_perc 与新版直方图 alias 都能解析。"""
    body = "\n".join(
        [
            "vllm:gpu_cache_usage_perc 0.2",
            'vllm:request_time_per_output_token_seconds_bucket{le="0.1"} 4',
            "vllm:request_time_per_output_token_seconds_sum 0.4",
            "vllm:request_time_per_output_token_seconds_count 4",
        ]
    )
    snap = agent_main._collect_metrics_snapshot(body)
    assert snap["kv_percent"] == 20.0
    assert set(snap["hist"]) == {"tpot"}
    assert snap["hist"]["tpot"]["count"] == 4.0


def test_collect_metrics_snapshot_preserves_small_kv_gauge():
    """科学计数法的小占用值不能在 Agent 解析或百分比换算时归零。"""
    snap = agent_main._collect_metrics_snapshot(
        'vllm:kv_cache_usage_perc{engine="0"} 5.2121338475985546e-05'
    )
    assert snap["kv_percent"] == pytest.approx(0.005212133847598555)


def test_collect_live_stats_reads_full_bounded_metrics_page(monkeypatch):
    """较大的 /metrics 不能沿用 512 KiB 上限而静默截掉 KV 指标。"""
    seen = {}

    def fake(url, timeout=3, limit=4096):
        seen["limit"] = limit
        return 200, PRE_BODY

    monkeypatch.setattr(agent_main, "_http_get_short", fake)
    backend, snap = agent_main._collect_live_stats("http://127.0.0.1:8888", 3)
    assert backend == "vllm"
    assert snap["kv_percent"] == 30.0
    assert seen["limit"] == 8 * 1024 * 1024


def test_api_inference_stats_returns_raw_snapshot_stateless(monkeypatch):
    """/api/inference/stats 无状态：每次返回原始累计快照（非差分），+Inf 桶上界为 null。

    不发送合成请求——_http_get_short 只用于读 /metrics。
    """
    from fastapi.testclient import TestClient

    bodies = iter([(200, PRE_BODY), (200, CUR_BODY)])
    monkeypatch.setattr(
        agent_main,
        "_http_get_short",
        lambda url, timeout=3, limit=4096: next(bodies),
    )
    client = TestClient(agent_main.app)

    r1 = client.post(
        "/api/inference/stats", json={"url_base": "http://127.0.0.1:8888"}, headers=AUTH
    )
    d1 = r1.json()
    assert d1["ok"] and d1["backend"] == "vllm"
    assert d1["generation_tokens_total"] == 100.0
    assert d1["request_success_total"] == 4.0  # stop(3)+abort(1)
    assert d1["kv_cache_percent"] == 30.0
    # 返回累计直方图快照；+Inf 桶上界归一化为 null（避免 JSON Infinity）
    ttft = d1["ttft"]
    assert ttft["count"] == 10.0
    assert ttft["buckets"][-1][0] is None
    assert "tokens_per_sec" not in d1
    assert "traffic" not in d1

    # 第二次调用返回新的累计值（160）而非差分（60）——证明 agent 无状态
    r2 = client.post(
        "/api/inference/stats", json={"url_base": "http://127.0.0.1:8888"}, headers=AUTH
    )
    d2 = r2.json()
    assert d2["generation_tokens_total"] == 160.0


def test_validate_project_accepts_compose_safe_names():
    """Compose v5 安全项目名（小写/数字/-/_）正常通过。"""
    for p in ("glm53-flash-nv", "a", "dsv4f_nv01", "0abc", "a_b-c9"):
        assert agent_main._validate_project(p) == p


def test_validate_project_rejects_dot_uppercase_space():
    """带点/大写/空格的项目名被 400 拒绝——这正是 glm5.3-flash-nv 发布
    在节点上被 Docker Compose v5 拒绝（502）的直接原因，Agent 应提前 400。"""
    from fastapi import HTTPException

    for p in (
        "glm5.3-flash-nv",
        ".", "..",
        "GLM-5.3-Flash",
        "Task",
        "a b",
        "-abc",
        "_abc",
        "a/b",
    ):
        with pytest.raises(HTTPException) as e:
            agent_main._validate_project(p)
        assert e.value.status_code == 400


def test_model_cache_repo_sha_verify(monkeypatch, tmp_path):
    """agent /api/model/cache?sha= 精确校验目标 commit：完整返回 verify_sha、
    missing 为空；sha 版本不完整返回 missing 清单（差量补齐依据）。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(agent_main, "DEFAULT_HF_CACHE", tmp_path)
    root = tmp_path / "hub" / "models--owner--repo"
    content = b"model-weights"
    meta = b"hello"
    digest = hashlib.sha256(content).hexdigest()
    meta_digest = hashlib.sha256(meta).hexdigest()
    blobs = root / "blobs"
    blobs.mkdir(parents=True)
    (blobs / digest).write_bytes(content)
    (blobs / meta_digest).write_bytes(meta)
    (root / "refs").mkdir()
    (root / "refs" / "main").write_text("commitA")
    # 两个快照共享同一权重 blob（内容寻址）：A 完整、B 缺 meta.json
    for sha in ("commitA", "commitB"):
        (root / "trees").mkdir(exist_ok=True)
        (root / "trees" / f"{sha}.json").write_text(json.dumps(
            {"model.bin": {"size": len(content), "blob_id": digest},
             "meta.json": {"size": len(meta), "blob_id": meta_digest}}))
        snap = root / "snapshots" / sha
        snap.mkdir(parents=True)
        (snap / "model.bin").symlink_to(f"../../blobs/{digest}")
        if sha == "commitA":
            (snap / "meta.json").symlink_to(f"../../blobs/{meta_digest}")

    client = TestClient(agent_main.app)

    r = client.get("/api/model/cache/owner/repo", params={"sha": "commitA"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["complete"] is True
    assert body["verify_sha"] == "commitA"
    assert body["missing"] == []
    assert body["truncated"] is False

    r = client.get("/api/model/cache/owner/repo", params={"sha": "commitB"}, headers=AUTH)
    body = r.json()
    assert body["complete"] is False
    assert body["verify_sha"] == "commitB"
    assert body["missing"] == ["meta.json"], body["missing"]
    assert body["truncated"] is False


def test_verify_snapshot_sha_truncation_flag(monkeypatch, tmp_path):
    """缺失超过上限（500）：missing 清单被截断并置 truncated=True，
    控制平面据此回退全量传输，而不是只补截断后的子集（否则快照永远不完整）。"""
    monkeypatch.setattr(agent_main, "DEFAULT_HF_CACHE", tmp_path)
    root = tmp_path / "hub" / "models--owner--big"
    (root / "trees").mkdir(parents=True)
    (root / "snapshots" / "commitX").mkdir(parents=True)
    (root / "snapshots" / "commitY").mkdir(parents=True)
    big = {f"file{i}.bin": {"size": 10, "blob_id": "b" * 40} for i in range(501)}
    (root / "trees" / "commitX.json").write_text(json.dumps(big))
    st = agent_main._verify_snapshot_sha("owner/big", "commitX")
    assert st["ok"] is False
    assert len(st["missing"]) == 500   # 清单被截断
    assert st["truncated"] is True

    just_500 = {f"file{i}.bin": {"size": 10, "blob_id": "b" * 40} for i in range(500)}
    (root / "trees" / "commitY.json").write_text(json.dumps(just_500))
    st = agent_main._verify_snapshot_sha("owner/big", "commitY")
    assert st["ok"] is False
    assert len(st["missing"]) == 500   # 恰好 500：不截断
    assert st["truncated"] is False

    # HTTP 层回归：endpoint 必须透传 truncated，否则控制平面永远收不到该标志
    from fastapi.testclient import TestClient
    client = TestClient(agent_main.app)
    r = client.get("/api/model/cache/owner/big", params={"sha": "commitX"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["truncated"] is True, body
    assert len(body["missing"]) == 500
    r = client.get("/api/model/cache/owner/big", params={"sha": "commitY"}, headers=AUTH)
    assert r.json()["truncated"] is False
