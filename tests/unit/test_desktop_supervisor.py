"""桌面 Supervisor：端口、进程清理、握手与失败路径。

不启动真实子进程：`_spawn` 的 Popen 与健康检查都换成本地假对象，
这样 Windows 无 WSL 也能覆盖启动/退出分支。
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from coding_agent.desktop import supervisor
from coding_agent.desktop.config import LauncherConfig

pytestmark = pytest.mark.regression


class _Stdin:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.closed = False

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _Proc:
    def __init__(self, lines: tuple[str, ...], *, timeouts: int = 0) -> None:
        self.stdin = _Stdin()
        self.stdout = iter(lines)
        self._timeouts = timeouts
        self.terminated = 0
        self.killed = 0

    def wait(self, timeout: float | None = None) -> int:
        if self._timeouts:
            self._timeouts -= 1
            raise subprocess.TimeoutExpired("fake", timeout)
        return 0

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1


def _config(**overrides) -> LauncherConfig:
    data = {
        "distro": "Ubuntu",
        "workspace": "/home/me/ws",
        "python": "/home/me/venv/bin/python",
    }
    data.update(overrides)
    return LauncherConfig(**data)


def test_env_port_validation(monkeypatch) -> None:
    assert supervisor._env_port("MISSING", 8790) == 8790
    monkeypatch.setenv("PORT", "not-a-number")
    assert supervisor._env_port("PORT", 8790) == 8790
    for bad in ("0", "70000", "-1"):
        monkeypatch.setenv("PORT", bad)
        assert supervisor._env_port("PORT", 8790) == 8790
    monkeypatch.setenv("PORT", "1234")
    assert supervisor._env_port("PORT", 8790) == 1234


def test_port_available_handles_bind_failure(monkeypatch) -> None:
    class _Busy:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def bind(self, address):
            raise OSError("busy")

    real_socket = socket.socket
    monkeypatch.setattr(
        supervisor.socket, "socket", lambda *a, **k: _Busy()
    )
    assert supervisor._port_available(8790) is False

    monkeypatch.setattr(supervisor.socket, "socket", real_socket)
    assert supervisor._port_available(0) is True


def test_stop_proc_variants() -> None:
    clean = _Proc(())
    supervisor._stop_proc(clean, timeout=1)  # type: ignore[arg-type]
    assert clean.terminated == 0

    slow = _Proc((), timeouts=1)
    supervisor._stop_proc(slow, timeout=1)  # type: ignore[arg-type]
    assert slow.terminated == 1
    assert slow.killed == 0

    stuck = _Proc((), timeouts=2)
    supervisor._stop_proc(stuck, timeout=1)  # type: ignore[arg-type]
    assert stuck.terminated == 1
    assert stuck.killed == 1


def test_post_swallows_transport_errors(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("down")

    monkeypatch.setattr(supervisor, "local_open", boom)
    supervisor._post(1, "tok", "/shutdown")


def test_control_prefix_prefers_packaged_binary(tmp_path, monkeypatch) -> None:
    exe = tmp_path / "CodingAgentControl.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        supervisor.sys, "executable", str(tmp_path / "python.exe")
    )
    assert supervisor._control_prefix() == [str(exe.resolve())]

    exe.unlink()
    assert supervisor._control_prefix() == [
        str(tmp_path / "python.exe"),
        "-m",
        "coding_agent.control_server",
    ]


def test_build_control_command_adds_migrate_and_write(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_CONTROL_DB_URL", "sqlite:///x.db")
    config = _config(allow_write=True)
    command = supervisor.build_control_command(config)
    assert "--migrate" in command
    assert "--write" in command


def test_spawn_handshake_success(tmp_path, monkeypatch) -> None:
    proc = _Proc(('{"event": "ready", "port": 4321}\n',))
    monkeypatch.setattr(
        supervisor.subprocess, "Popen", lambda *a, **k: proc
    )
    checked: list[str] = []
    monkeypatch.setattr(
        supervisor, "wait_health", lambda url, timeout_s: checked.append(url)
    )

    result = supervisor._spawn(
        ["cmd"],
        token="tok",
        event="ready",
        port=4321,
        log_path=tmp_path / "x.log",
    )
    assert result is proc
    assert proc.stdin.written == ["tok\n"]
    assert proc.stdin.closed is True
    assert checked == ["http://127.0.0.1:4321/healthz"]


def test_spawn_without_handshake_cleans_up(tmp_path, monkeypatch) -> None:
    proc = _Proc((), timeouts=2)
    monkeypatch.setattr(
        supervisor.subprocess, "Popen", lambda *a, **k: proc
    )

    with pytest.raises(RuntimeError, match="无输出"):
        supervisor._spawn(
            ["cmd"],
            token="tok",
            event="ready",
            port=4321,
            log_path=tmp_path / "x.log",
        )
    assert proc.killed == 1


def test_start_agent_rejects_invalid_config(tmp_path) -> None:
    config = _config(distro="", workspace="relative", python="python3")
    with pytest.raises(RuntimeError, match="发行版"):
        supervisor.start_agent(config, log_path=tmp_path / "x.log")


def test_start_agent_uses_env_port(tmp_path, monkeypatch) -> None:
    directories: list[str] = []
    calls: list[tuple[list[str], int]] = []
    monkeypatch.setenv("AGENT_DESKTOP_AGENT_PORT", "41234")
    monkeypatch.setattr(
        supervisor, "ensure_dir", lambda distro, path: directories.append(path)
    )
    monkeypatch.setattr(supervisor, "home", lambda distro: "/home/me")
    monkeypatch.setattr(supervisor, "_port_available", lambda port: True)
    monkeypatch.setattr(
        supervisor,
        "_spawn",
        lambda command, **kwargs: calls.append((command, kwargs["port"]))
        or object(),
    )

    server = supervisor.start_agent(
        _config(), log_path=tmp_path / "x.log"
    )
    assert server.port == 41234
    assert calls[0][1] == 41234
    assert any("audit" in path for path in directories)
    assert any("steering" in path for path in directories)


def test_start_control_injects_agent_env(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(supervisor, "_port_available", lambda port: True)

    def fake_spawn(command, *, env=None, **kwargs):
        captured["command"] = command
        captured["env"] = env
        return object()

    monkeypatch.setattr(supervisor, "_spawn", fake_spawn)
    server = supervisor.start_control(
        _config(),
        agent_port=8790,
        agent_token="agent-token",
        log_path=tmp_path / "control.log",
        port=8780,
        token="control-token",
    )

    assert server.port == 8780
    assert server.url == "http://127.0.0.1:8780/#token=control-token"
    env = captured["env"]
    assert env["AGENT_INTERNAL_URL"] == "http://127.0.0.1:8790"
    assert env["AGENT_INTERNAL_TOKEN"] == "agent-token"


def test_start_control_rejects_occupied_port(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(supervisor, "_port_available", lambda port: False)
    with pytest.raises(RuntimeError, match="已被占用"):
        supervisor.start_control(
            _config(),
            agent_port=8790,
            agent_token="t",
            log_path=tmp_path / "control.log",
            port=8780,
        )


def test_server_stop_signals_shutdown(monkeypatch) -> None:
    posted: list[tuple[int, str, str]] = []
    stopped: list[object] = []
    monkeypatch.setattr(
        supervisor,
        "_post",
        lambda port, token, path: posted.append((port, token, path)),
    )
    monkeypatch.setattr(
        supervisor,
        "_stop_proc",
        lambda proc, timeout: stopped.append(proc),
    )

    proc = object()
    agent = supervisor.AgentServer(
        proc=proc, token="t", port=8790, log_path=Path("agent.log")
    )
    agent.stop()
    assert posted == [
        (8790, "t", "/v1/cancel_all"),
        (8790, "t", "/v1/shutdown"),
    ]

    posted.clear()
    control = supervisor.ControlServer(
        proc=proc, token="t", port=8780, log_path=Path("control.log")
    )
    control.stop()
    assert posted == [
        (8780, "t", "/api/desktop/cancel_all"),
        (8780, "t", "/api/desktop/shutdown"),
    ]
    assert stopped == [proc, proc]


def test_default_workspace(monkeypatch) -> None:
    monkeypatch.setattr(supervisor, "home", lambda distro: "/home/me")
    assert (
        supervisor.default_workspace(_config(), "demo")
        == "/home/me/.coding-agent/workspaces/demo"
    )


def test_launcher_config_roundtrip_workspace_fields(
    tmp_path, monkeypatch
) -> None:
    import json

    from coding_agent.desktop import config as desktop_config

    path = tmp_path / "launcher.json"
    monkeypatch.setattr(desktop_config, "config_path", lambda: path)
    cfg = _config(
        workspace_id="wid",
        recent_workspaces=["a", "b"],
        control_db_url="mysql+pymysql://u:p@127.0.0.1/coding_agent",
    )
    cfg.save()
    loaded = LauncherConfig.load()
    assert loaded is not None
    assert loaded.workspace_id == "wid"
    assert loaded.recent_workspaces == ["a", "b"]
    assert loaded.control_db_url.startswith("mysql+pymysql")
    assert json.loads(path.read_text(encoding="utf-8"))["workspace_id"] == "wid"


def test_build_control_command_uses_config_db(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_CONTROL_DB_URL", raising=False)
    command = supervisor.build_control_command(
        _config(control_db_url="sqlite:///control.db")
    )
    assert "--migrate" in command


def test_start_control_injects_config_db(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(supervisor, "_port_available", lambda port: True)
    monkeypatch.setattr(
        supervisor,
        "_spawn",
        lambda command, *, env=None, **kwargs: captured.update(
            {"command": command, "env": env}
        )
        or object(),
    )
    supervisor.start_control(
        _config(control_db_url="mysql+pymysql://u:p@127.0.0.1/db"),
        agent_port=8790,
        agent_token="agent-token",
        log_path=tmp_path / "control.log",
        port=8780,
        token="control-token",
    )
    assert captured["env"]["AGENT_CONTROL_DB_URL"].startswith("mysql")
    assert "--migrate" in captured["command"]


def test_start_agent_fails_closed_on_hardening(
    tmp_path, monkeypatch
) -> None:
    from coding_agent.desktop import hardening

    monkeypatch.setenv("AGENT_WORKSPACE_REGISTRY", "true")
    monkeypatch.setattr(supervisor, "ensure_dir", lambda *a, **k: None)
    monkeypatch.setattr(
        hardening,
        "check",
        lambda *a, **k: hardening.HardeningReport(
            ok=False, problems=["root"]
        ),
    )
    with pytest.raises(RuntimeError, match="WSL 加固"):
        supervisor.start_agent(
            _config(),
            log_path=tmp_path / "agent.log",
            port=8791,
            token="t",
        )


def test_old_launcher_json_is_compatible(tmp_path, monkeypatch) -> None:
    import json

    from coding_agent.desktop import config as desktop_config

    path = tmp_path / "launcher.json"
    monkeypatch.setattr(desktop_config, "config_path", lambda: path)
    path.write_text(
        json.dumps(
            {
                "distro": "Ubuntu",
                "workspace": "/home/me/ws",
                "python": "/home/me/venv/bin/python",
                "allow_write": True,
            }
        ),
        encoding="utf-8",
    )
    loaded = LauncherConfig.load()
    assert loaded is not None
    assert loaded.workspace_id == ""
    assert loaded.recent_workspaces == []


def test_agent_env_enables_workspace_defaults(monkeypatch) -> None:
    monkeypatch.setattr(supervisor, "home", lambda distro: "/home/me")
    for key in supervisor._WORKSPACE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_WORKSPACE_REGISTRY", "true")
    env = supervisor._agent_env(_config())
    assert env["AGENT_WORKSPACE_ROOT"] == "/home/me/agent-ws/users"
    assert env["AGENT_WORKSPACE_MOUNT_ROOTS"] == "/mnt/"
    assert env["AGENT_WORKSPACE_IMPORT_MODE"] == "copy"


def test_agent_env_passes_explicit_values(monkeypatch) -> None:
    monkeypatch.setattr(supervisor, "home", lambda distro: "/home/me")
    for key in supervisor._WORKSPACE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", "/srv/ws")
    monkeypatch.setenv("AGENT_WORKSPACE_MOUNT_ROOTS", "/mnt/e")
    env = supervisor._agent_env(_config())
    assert env["AGENT_WORKSPACE_ROOT"] == "/srv/ws"
    assert env["AGENT_WORKSPACE_MOUNT_ROOTS"] == "/mnt/e"
    assert "AGENT_WORKSPACE_REGISTRY" not in env
