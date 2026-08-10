"""Agent 侧回归：任务表 TTL 清理、model_pull Range 断点续传、空 digest 校验、日志流切分。"""

import asyncio
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
    """进度条 \r 刷新链：每段 update=True，末尾 \n 段视为刷新行最终态（仍 update）。"""
    segs, buf, prev = agent_main._log_segments(b"\r10%\r20%\r100%\n", b"", False)
    assert segs == [(b"10%", True), (b"20%", True), (b"100%", True)]
    assert buf == b"" and prev is True


def test_log_segments_partial_buffering():
    """分片到达：未闭合段留缓冲，\r 到达后按刷新行处理，\n 收尾段仍覆盖。"""
    segs, buf, prev = agent_main._log_segments(b"Loading\r12%", b"", False)
    assert segs == [(b"Loading", True)]
    assert buf == b"12%"
    segs2, buf2, prev2 = agent_main._log_segments(b"\r34%\n", buf, prev)
    assert segs2 == [(b"12%", True), (b"34%", True)]
    assert buf2 == b""


def test_log_segments_empty_skipped():
    """连续分隔符的空段跳过且不改变 update 标志。"""
    segs, buf, prev = agent_main._log_segments(b"a\n\r\r\n\n", b"", True)
    assert segs == [(b"a", True)]  # prev=True -> \n 段继承为 update
    assert buf == b"" and prev is True


def test_resolve_pull_url_relative_from_client_ip():
    """相对路径 -> 用下发请求来源 IP 补全控制端地址（http://<ip>:8000）。"""
    assert agent_main._resolve_pull_url(
        "192.168.198.5", "/api/models/files/x?relpath=a&token=t"
    ) == "http://192.168.198.5:8000/api/models/files/x?relpath=a&token=t"


def test_resolve_pull_url_adds_leading_slash():
    assert agent_main._resolve_pull_url(
        "10.0.1.5", "api/images/archive/1?token=t"
    ) == "http://10.0.1.5:8000/api/images/archive/1?token=t"


def test_resolve_pull_url_full_url_restricted():
    """绝对 URL 仅允许：控制平面来源 IP:8000 或本机回环；其余一律 400（防 SSRF）。"""
    assert agent_main._resolve_pull_url(
        "192.168.198.5", "http://192.168.198.5:8000/api/x"
    ) == "http://192.168.198.5:8000/api/x"
    # 回环端口不受限（测试/本地回环用）
    assert agent_main._resolve_pull_url(
        "192.168.198.5", "http://127.0.0.1:9000/api/x"
    ) == "http://127.0.0.1:9000/api/x"
    # 非控制平面的内网/公网地址一律拒绝
    with pytest.raises(Exception):
        agent_main._resolve_pull_url("192.168.198.5", "http://10.0.0.9:8000/api/x")
    with pytest.raises(Exception):
        agent_main._resolve_pull_url("192.168.198.5", "https://example.com/f")


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
        r = client.post("/api/model/pull", json={
            "repo": "owner/repo", "relpath": rel,
            "url": f"http://127.0.0.1:{port}/f", "size": len(content),
        }, headers=AUTH)
        assert r.status_code == 200, r.text
        assert _RangeHandler.last_range == f"bytes={500 * 1024}-"  # 携带续传头
        target = hf / "hub" / "models--owner--repo" / rel
        assert target.read_bytes() == content  # 续传后内容完整
        assert not part.exists()  # 已 rename 收尾
    finally:
        server.shutdown()
        thread.join()


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
