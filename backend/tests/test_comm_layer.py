"""通讯层回归：超时组装（connect 固定 5s）、统一异常映射、SSH exec 大 stderr 不死锁。"""

import select as _select

import httpx
import pytest
from fastapi import HTTPException

from app.services import agent_client
from app.services.ssh_client import exec as ssh_exec


def test_timeout_keeps_connect_short():
    """长任务总超时不放大连接超时（不可达节点仍快速失败）。"""
    t = agent_client._timeout(3600)
    assert t.connect == 5.0
    assert t.read == 3600
    d = agent_client._timeout(None)
    assert d.connect == 5.0


def test_map_agent_error():
    req = httpx.Request("GET", "http://x")

    e404 = httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, text="容器不存在"))
    exc = agent_client.map_agent_error(e404)
    assert isinstance(exc, HTTPException) and exc.status_code == 404

    e502 = httpx.HTTPStatusError("502", request=req, response=httpx.Response(502, text="compose 失败"))
    exc = agent_client.map_agent_error(e502)
    assert exc.status_code == 502 and "compose" in str(exc.detail)

    exc = agent_client.map_agent_error(httpx.ConnectError("连接失败"))
    assert exc.status_code == 502


# ---------- SSH exec 大 stderr 不死锁（修复前顺序读会死锁/超时） ----------


class _FakeChannel:
    def __init__(self, out: bytes, err: bytes):
        self._out = out
        self._err = err

    def recv_ready(self):
        return bool(self._out)

    def recv_stderr_ready(self):
        return bool(self._err)

    def recv(self, n):
        data, self._out = self._out[:n], self._out[n:]
        return data

    def recv_stderr(self, n):
        data, self._err = self._err[:n], self._err[n:]
        return data

    def exit_status_ready(self):
        return not self._out and not self._err

    def recv_exit_status(self):
        return 0


class _FakeStdout:
    def __init__(self, ch):
        self.channel = ch


class _FakeClient:
    def __init__(self, ch):
        self.ch = ch

    def exec_command(self, command, timeout=None):
        return None, _FakeStdout(self.ch), _FakeStdout(self.ch)


def test_exec_reads_large_stderr(monkeypatch):
    """stderr 输出 200KB（远超 channel 缓冲）时 exec 仍完整读取、不阻塞。"""
    ch = _FakeChannel(b"stdout-line\n", b"e" * 200000)
    monkeypatch.setattr(_select, "select", lambda *a, **k: ([ch], [], []))
    out, err, rc = ssh_exec(_FakeClient(ch), "cmd")
    assert rc == 0
    assert out == "stdout-line\n"
    assert len(err) == 200000


def test_exec_reads_both_streams(monkeypatch):
    ch = _FakeChannel(b"out" * 50000, b"err" * 50000)
    monkeypatch.setattr(_select, "select", lambda *a, **k: ([ch], [], []))
    out, err, rc = ssh_exec(_FakeClient(ch), "cmd")
    assert out == "out" * 50000
    assert err == "err" * 50000
