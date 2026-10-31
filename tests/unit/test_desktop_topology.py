"""Phase D2：Windows A2 + WSL A3 的启动拓扑与看门狗。"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from ubismart import AGENT_API_VERSION, __version__
from ubismart.config import Settings
from ubismart.control.protocol import AgentMeta
from ubismart.desktop.config import LauncherConfig
from ubismart.desktop.watchdog import WatchTarget, supervise_many
from ubismart.servers import agent_server


def test_agent_server_cancel_all_and_shutdown(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        agent_data_root=str(tmp_path / "agent"),
    )
    client = TestClient(agent_server.create_agent_app(settings, token="tok"))
    assert client.post("/v1/cancel_all").status_code == 401
    headers = {"Authorization": "Bearer tok"}
    assert client.post("/v1/cancel_all", headers=headers).json() == {
        "cancelled": 0
    }
    assert client.post("/v1/shutdown", headers=headers).json() == {
        "ok": True
    }


def _config() -> LauncherConfig:
    return LauncherConfig(
        distro="Ubuntu",
        workspace="/home/me/ws",
        python="/home/me/venv/bin/python",
    )


def test_start_control_injects_agent_endpoint(
    tmp_path, monkeypatch
) -> None:
    import ubismart.desktop.supervisor as supervisor

    captured: dict = {}

    def fake_spawn(
        command, *, token, event, port, log_path, env=None, timeout=20.0
    ):
        captured.update(
            command=command,
            token=token,
            event=event,
            port=port,
            env=env,
        )
        return object()

    monkeypatch.setattr(supervisor, "_port_available", lambda port: True)
    monkeypatch.setattr(supervisor, "_spawn", fake_spawn)

    server = supervisor.start_control(
        _config(),
        agent_port=8790,
        agent_token="agent-token",
        log_path=tmp_path / "control.log",
        port=8780,
    )

    assert server.port == 8780
    assert captured["event"] == "coding_agent_control_plane"
    assert captured["port"] == 8780
    assert captured["env"]["AGENT_INTERNAL_URL"] == "http://127.0.0.1:8790"
    assert captured["env"]["AGENT_INTERNAL_TOKEN"] == "agent-token"
    assert "--desktop" in captured["command"]
    assert "--token-stdin" in captured["command"]


class _Proc:
    def __init__(self, code: int | None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


class _Server:
    def __init__(self, code: int | None) -> None:
        self.proc = _Proc(code)


class _Event:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float) -> bool:
        return self.stopped


def test_supervise_many_restarts_crashed_target() -> None:
    event = _Event()
    restarted: list[str] = []

    def restart():
        restarted.append("agent")
        event.stopped = True
        return _Server(None)

    target = WatchTarget("agent", _Server(1), restart)
    result = supervise_many(
        [target],
        event,
        poll_interval=0,
        backoff=(0,),
        sleep=lambda seconds: None,
    )
    assert result == "stopped"
    assert restarted == ["agent"]


def test_supervise_many_reports_failed_target() -> None:
    event = _Event()

    def restart():
        raise RuntimeError("always")

    target = WatchTarget("control", _Server(1), restart)
    result = supervise_many(
        [target],
        event,
        poll_interval=0,
        backoff=(0, 0),
        sleep=lambda seconds: None,
    )
    assert result == "failed:control"


class _FakeAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def probe(self) -> AgentMeta:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("agent down")
        return AgentMeta.from_dict(
            {
                "service": "agent-server",
                "version": __version__,
                "api_version": AGENT_API_VERSION,
                "execution_backend": "local",
                "capabilities": {"run_lifecycle": ["runs"]},
            }
        )

    async def aclose(self) -> None:
        return None


def test_a2_probe_recovers_after_agent_restart(
    tmp_path, monkeypatch
) -> None:
    from ubismart import web
    from ubismart.web import app as web_app

    monkeypatch.setattr(web_app, "AGENT_PROBE_INTERVAL_S", 0.01)
    settings = Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        agent_data_root=str(tmp_path / "agent"),
    )
    fake = _FakeAgent()
    app = web.create_app(settings, agent_client=fake)

    with TestClient(app) as client:
        deadline = time.monotonic() + 2.0
        state = "unknown"
        while time.monotonic() < deadline:
            state = client.get("/healthz").json()["agent_state"]
            if state == "ok":
                break
            time.sleep(0.01)

    assert state == "ok"
    assert fake.calls >= 2
