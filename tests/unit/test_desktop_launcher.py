"""Phase 1 启动器：路径转换、命令构造与本地配置。"""

from __future__ import annotations

import sys

import pytest

from coding_agent.desktop.config import LauncherConfig
from coding_agent.desktop.supervisor import build_command, build_control_command
from coding_agent.desktop.wsl import to_wsl_path


def test_to_wsl_path_handles_windows_and_posix() -> None:
    assert to_wsl_path(r"D:\proj\a") == "/mnt/d/proj/a"
    assert to_wsl_path("C:/tmp/x") == "/mnt/c/tmp/x"
    assert to_wsl_path("/home/me/proj") == "/home/me/proj"
    assert (
        to_wsl_path(r"\\wsl$\Ubuntu\home\me\proj")
        == "/home/me/proj"
    )
    # Git Bash 路径转换的绕行前缀。
    assert to_wsl_path("wsl:/home/me/proj") == "/home/me/proj"
    assert to_wsl_path("wsl://home/me/proj") == "/home/me/proj"


def test_build_agent_command_uses_stdin_token_and_port(monkeypatch) -> None:
    monkeypatch.setattr(
        "coding_agent.desktop.supervisor.home",
        lambda distro: "/home/me",
    )
    config = LauncherConfig(
        distro="Ubuntu",
        workspace="/home/me/ws",
        python="/home/me/.coding-agent/venv/bin/python",
    )
    command = build_command(config)
    assert command[:6] == [
        "wsl.exe",
        "-d",
        "Ubuntu",
        "--cd",
        "/home/me/ws",
        "--",
    ]
    assert "coding_agent.servers.agent_server" in command
    assert "--port" in command and command[command.index("--port") + 1] == "0"
    explicit = build_command(config, port=54321)
    assert explicit[explicit.index("--port") + 1] == "54321"
    assert "--token-stdin" in command
    assert "AGENT_EXECUTION_BACKEND=local" in command
    # workspace/写权限由每次运行的 AgentContext 决定，不再写进 A3 命令。
    assert "--workspace" not in command
    assert "--write" not in command


def test_build_control_command_enables_desktop() -> None:
    config = LauncherConfig(
        distro="Ubuntu",
        workspace="/home/me/ws",
        python="/home/me/venv/bin/python",
    )
    command = build_control_command(config, port=8780)
    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "coding_agent.servers.control_server"]
    assert "--desktop" in command
    assert "--token-stdin" in command
    assert command[command.index("--port") + 1] == "8780"
    assert "--write" not in command

    config.allow_write = True
    assert "--write" in build_control_command(config)


def test_run_retries_transient_wsl_startup_errors(monkeypatch) -> None:
    import subprocess

    import coding_agent.desktop.wsl as wsl

    codes = iter([-1073741502, 0])
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args, returncode=next(codes), stdout=b"", stderr=b""
        )

    monkeypatch.setattr(wsl.subprocess, "run", fake_run)
    monkeypatch.setattr(wsl.time, "sleep", lambda seconds: None)

    result = wsl.run("CodingAgent", "true")
    assert result.returncode == 0
    assert len(calls) == 2


def test_start_agent_uses_explicit_port(tmp_path, monkeypatch) -> None:
    import coding_agent.desktop.supervisor as supervisor

    calls: list[tuple[list[str], int, str]] = []

    def fake_spawn(
        command, *, token, event, port, log_path, env=None, timeout=20.0
    ):
        calls.append((command, port, event))
        return object()

    monkeypatch.setattr(supervisor, "ensure_dir", lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "home", lambda distro: "/home/me")
    monkeypatch.setattr(supervisor, "_port_available", lambda port: True)
    monkeypatch.setattr(supervisor, "_spawn", fake_spawn)

    config = LauncherConfig(
        distro="Ubuntu",
        workspace="/home/me/ws",
        python="/home/me/venv/bin/python",
    )
    server = supervisor.start_agent(
        config, log_path=tmp_path / "agent.log", port=41001
    )
    assert server.port == 41001
    assert calls[0][1] == 41001
    assert calls[0][2] == "coding_agent_agent_server"
    assert "coding_agent.servers.agent_server" in calls[0][0]


def test_start_agent_rejects_occupied_port(tmp_path, monkeypatch) -> None:
    import coding_agent.desktop.supervisor as supervisor

    monkeypatch.setattr(supervisor, "ensure_dir", lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "home", lambda distro: "/home/me")
    monkeypatch.setattr(supervisor, "_port_available", lambda port: False)

    config = LauncherConfig(
        distro="Ubuntu",
        workspace="/home/me/ws",
        python="/home/me/venv/bin/python",
    )
    with pytest.raises(RuntimeError, match="已被占用"):
        supervisor.start_agent(
            config, log_path=tmp_path / "agent.log", port=41001
        )


def test_launcher_config_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    config = LauncherConfig(
        distro="Ubuntu",
        workspace="/home/me/ws",
        python="/home/me/.coding-agent/venv/bin/python",
    )
    config.save()
    assert LauncherConfig.load() == config
    assert config.validate() == []

    assert LauncherConfig(
        distro="", workspace="relative", python="python3"
    ).validate()
