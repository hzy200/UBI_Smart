"""Windows 桌面侧的双进程 Supervisor（Phase D2）。

A3（Agent Server）跑在 WSL 里，A2（控制面）跑在 Windows 本机；两者只通过
loopback + 短期内部 token 通信。checkpoint、工具、沙箱全部留在 WSL。

端口固定，便于 A2 的 `AGENT_INTERNAL_URL` 在 A3 重启后仍有效；token 每次
启动随机生成，仅保存在 supervisor 内存并复用给重启后的进程。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from coding_agent.desktop.config import LauncherConfig
from coding_agent.desktop.http import local_open, wait_health
from coding_agent.desktop.wsl import ensure_dir, home

_LOG = logging.getLogger("coding_agent.desktop")

DEFAULT_AGENT_PORT = 8790
DEFAULT_CONTROL_PORT = 8780


@dataclass
class AgentServer:
    proc: subprocess.Popen
    token: str
    port: int
    log_path: Path

    def stop(self, *, timeout: float = 10.0) -> None:
        _post(self.port, self.token, "/v1/cancel_all")
        _post(self.port, self.token, "/v1/shutdown")
        _stop_proc(self.proc, timeout=timeout)


@dataclass
class ControlServer:
    proc: subprocess.Popen
    token: str
    port: int
    log_path: Path

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#token={self.token}"

    def stop(self, *, timeout: float = 10.0) -> None:
        for path in ("/api/desktop/cancel_all", "/api/desktop/shutdown"):
            _post(self.port, self.token, path)
        _stop_proc(self.proc, timeout=timeout)


def _post(port: int, token: str, path: str) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with local_open(request, 8) as response:
            response.read()
    except Exception:  # noqa: BLE001 - 服务可能已经停止
        pass


def _stop_proc(proc: subprocess.Popen, *, timeout: float) -> None:
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _env_port(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if 0 < value < 65536 else default


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _control_prefix() -> list[str]:
    """打包后优先用同目录的 CodingAgentControl.exe，开发模式用 -m。"""
    exe = Path(sys.executable).resolve().parent / "CodingAgentControl.exe"
    if exe.is_file():
        return [str(exe)]
    return [sys.executable, "-m", "coding_agent.control_server"]


def _agent_env(config: LauncherConfig) -> dict[str, str]:
    data_root = f"{home(config.distro)}/.coding-agent"
    return {
        "AGENT_AUDIT_DIR": f"{data_root}/audit",
        "AGENT_CHECKPOINT_PATH": f"{data_root}/checkpoints.sqlite",
        "AGENT_STEERING_DIR": f"{data_root}/steering",
        "AGENT_EXECUTION_BACKEND": "local",
        "PYTHONIOENCODING": "utf-8",
    }


def build_command(
    config: LauncherConfig, *, port: int = 0
) -> list[str]:
    """WSL 内 A3（Agent Server）的启动命令。"""
    env = _agent_env(config)
    return [
        "wsl.exe",
        "-d",
        config.distro,
        "--cd",
        config.workspace,
        "--",
        "env",
        *[f"{key}={value}" for key, value in env.items()],
        config.python,
        "-m",
        "coding_agent.agent_server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--token-stdin",
        "--log-level",
        "warning",
    ]


def build_control_command(
    config: LauncherConfig, *, port: int = 0
) -> list[str]:
    """Windows 本机 A2（控制面）的启动命令。"""
    command = [
        *_control_prefix(),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--token-stdin",
        "--desktop",
        "--log-level",
        "warning",
    ]
    if os.environ.get("AGENT_CONTROL_DB_URL", "").strip():
        command.append("--migrate")
    if config.allow_write:
        command.append("--write")
    return command


def _spawn(
    command: list[str],
    *,
    token: str,
    event: str,
    port: int,
    log_path: Path,
    env: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> subprocess.Popen:
    log_handle = log_path.open("a", encoding="utf-8")
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(token + "\n")
    proc.stdin.flush()
    proc.stdin.close()

    handshake: queue.Queue[dict | Exception] = queue.Queue()
    tail: list[str] = []

    def reader() -> None:
        handshake_sent = False
        assert proc.stdout is not None
        for line in proc.stdout:
            log_handle.write(line)
            log_handle.flush()
            tail.append(line.rstrip())
            del tail[:-30]
            if not handshake_sent:
                try:
                    payload = json.loads(line)
                except ValueError:
                    payload = {}
                if payload.get("event") == event:
                    handshake.put(payload)
                    handshake_sent = True
                    continue
        if not handshake_sent:
            handshake.put(RuntimeError(f"{event} 未返回握手"))
        handshake.put(RuntimeError("server 已退出"))
        log_handle.close()

    threading.Thread(target=reader, name=f"{event}-log", daemon=True).start()
    try:
        payload = handshake.get(timeout=timeout)
        if isinstance(payload, Exception):
            raise payload
        actual = int(payload.get("port") or port)
        wait_health(f"http://127.0.0.1:{actual}/healthz", timeout_s=15)
    except Exception:
        _stop_proc(proc, timeout=5)
        detail = "\n".join(tail[-8:])
        raise RuntimeError(detail or "无输出") from None
    return proc


def start_agent(
    config: LauncherConfig,
    *,
    log_path: Path,
    port: int | None = None,
    token: str | None = None,
) -> AgentServer:
    """启动 WSL 内 A3；端口固定，失败直接报错而不是换端口。"""
    problems = config.validate()
    if problems:
        raise RuntimeError("；".join(problems))
    if config.workspace.startswith("/"):
        ensure_dir(config.distro, config.workspace)
    data_root = f"{home(config.distro)}/.coding-agent"
    for sub in ("audit", "steering"):
        ensure_dir(config.distro, f"{data_root}/{sub}")

    chosen = (
        port
        if port is not None
        else _env_port("AGENT_DESKTOP_AGENT_PORT", DEFAULT_AGENT_PORT)
    )
    if not _port_available(chosen):
        raise RuntimeError(
            f"端口 {chosen} 已被占用；请设置 AGENT_DESKTOP_AGENT_PORT 后重试"
        )

    token = token or secrets.token_urlsafe(32)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _spawn(
        build_command(config, port=chosen),
        token=token,
        event="coding_agent_agent_server",
        port=chosen,
        log_path=log_path,
    )
    return AgentServer(
        proc=proc, token=token, port=chosen, log_path=log_path
    )


def start_control(
    config: LauncherConfig,
    *,
    agent_port: int,
    agent_token: str,
    log_path: Path,
    port: int | None = None,
    token: str | None = None,
) -> ControlServer:
    """启动 Windows 本机 A2，并把 A3 的地址与 token 注入其环境。"""
    chosen = (
        port
        if port is not None
        else _env_port("AGENT_DESKTOP_CONTROL_PORT", DEFAULT_CONTROL_PORT)
    )
    if not _port_available(chosen):
        raise RuntimeError(
            f"端口 {chosen} 已被占用；请设置 AGENT_DESKTOP_CONTROL_PORT 后重试"
        )

    env = os.environ.copy()
    env.update(
        {
            "AGENT_INTERNAL_URL": f"http://127.0.0.1:{agent_port}",
            "AGENT_INTERNAL_TOKEN": agent_token,
            "PYTHONIOENCODING": "utf-8",
        }
    )
    token = token or secrets.token_urlsafe(32)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _spawn(
        build_control_command(config, port=chosen),
        token=token,
        event="coding_agent_control_plane",
        port=chosen,
        log_path=log_path,
        env=env,
    )
    return ControlServer(
        proc=proc, token=token, port=chosen, log_path=log_path
    )


def default_workspace(config: LauncherConfig, name: str = "default") -> str:
    root = home(config.distro)
    return f"{root}/.coding-agent/workspaces/{name}"
