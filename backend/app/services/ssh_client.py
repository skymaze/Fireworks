"""基于 paramiko 的 SSH 封装（部署 Agent / 网络配置用）。"""

import io
import os
import select
from pathlib import Path

import paramiko

from ..models import Node


def _load_key(key_text: str):
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(key_text))
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("无法解析 SSH 私钥（支持 Ed25519 / RSA / ECDSA）")


def connect(node: Node, timeout: int = 15) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = {
        "port": node.ssh_port,
        "username": node.ssh_username,
        "timeout": timeout,
        "look_for_keys": False,
        "allow_agent": False,
    }
    if node.ssh_auth_type == "key" and node.ssh_key:
        kwargs["pkey"] = _load_key(node.ssh_key)
    else:
        kwargs["password"] = node.ssh_password
    client.connect(node.ip, **kwargs)
    return client


def exec(
    client: paramiko.SSHClient, command: str, timeout: int = 60,
    input_data: str | None = None,
):
    """执行命令，返回 (stdout, stderr, rc)。

    用 select 交替读取 stdout/stderr，避免任一流输出超过 channel 缓冲
    （~64KB）时顺序读造成死锁（远端进程阻塞在写、本地等 EOF）。
    """
    stdin, stdout, _stderr = client.exec_command(command, timeout=timeout)
    if input_data is not None:
        stdin.write(input_data)
        stdin.flush()
        stdin.channel.shutdown_write()
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    chan = stdout.channel
    while True:
        # 有数据就读，无数据但进程已退出则收尾；超时防万一
        ready, _, _ = select.select([chan], [], [], timeout)
        if ready:
            if chan.recv_ready():
                out_chunks.append(chan.recv(65536).decode("utf-8", "replace"))
            if chan.recv_stderr_ready():
                err_chunks.append(chan.recv_stderr(65536).decode("utf-8", "replace"))
        if chan.exit_status_ready():
            # 进程已退出：清空剩余缓冲后结束
            while chan.recv_ready():
                out_chunks.append(chan.recv(65536).decode("utf-8", "replace"))
            while chan.recv_stderr_ready():
                err_chunks.append(chan.recv_stderr(65536).decode("utf-8", "replace"))
            break
    rc = chan.recv_exit_status()
    return "".join(out_chunks), "".join(err_chunks), rc


def sftp_put(client: paramiko.SSHClient, local_path: str, remote_path: str):
    """分块上传（4MB/块），断点续传：远端 .part 已有字节数则对齐后追加。

    覆盖大文件场景（agent 镜像 tar ~150MB）；中断后重试复用已传部分。
    rename 前删除已存在目标：部分 sftp-server 对覆盖已有文件的 rename 返回失败。
    """
    local_size = Path(local_path).stat().st_size
    sftp = client.open_sftp()
    try:
        part = remote_path + ".part"

        def _finalize():
            try:
                sftp.remove(remote_path)  # 目标已存在（旧部署残留）时先删，rename 不覆盖
            except IOError:
                pass
            sftp.rename(part, remote_path)

        try:
            have = sftp.stat(part).st_size
        except IOError:
            have = 0
        if have >= local_size:
            _finalize()
            return
        with open(local_path, "rb") as f, sftp.open(part, "ab" if have else "wb") as rf:
            if have:
                f.seek(have)
            while True:
                chunk = f.read(4 << 20)
                if not chunk:
                    break
                rf.write(chunk)
        _finalize()
    finally:
        sftp.close()


def sftp_put_dir(client: paramiko.SSHClient, local_dir: str, remote_dir: str):
    """递归上传目录（保持相对路径），每文件走 sftp_put 分块续传。

    用于上传 agent 离线依赖 wheelhouse（wheels/<py版本>/ 子目录结构）。
    """
    sftp = client.open_sftp()
    try:
        for root, _dirs, files in os.walk(local_dir):
            rel = Path(root).relative_to(local_dir)
            target = Path(remote_dir) / rel
            try:
                sftp.stat(str(target))
            except IOError:
                sftp.mkdir(str(target))
            for f in files:
                sftp_put(client, str(Path(root) / f), str(target / f))
    finally:
        sftp.close()
