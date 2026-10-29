"""A3/A0 进程入口：参数解析、token 来源与握手输出。

`uvicorn.Server` 换成本地假对象：这里验的是入口装配，不真的监听端口。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.servers import agent_server, server

pytestmark = pytest.mark.regression


class _FakeServer:
    last: _FakeServer | None = None

    def __init__(self, config) -> None:
        self.config = config
        self.should_exit = False
        self.ran = False
        _FakeServer.last = self

    def handle_exit(self, sig, frame) -> None:
        self.should_exit = True

    def run(self) -> None:
        self.ran = True
        # 桌面关窗时通过 app.state.shutdown 请求退出。
        self.config.app.state.shutdown()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        wsl_workspace=str(tmp_path / "ws"),
        web_data_root=str(tmp_path / "web-data"),
        control_db_url=f"sqlite:///{tmp_path / 'control.db'}",
    )


def test_legacy_server_parser_defaults() -> None:
    args = server._parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.write is False


def test_legacy_server_free_port_is_bindable() -> None:
    port = server._free_port("127.0.0.1")
    assert 0 < port < 65536


def test_legacy_server_main_prints_handshake(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(server, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(server.uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(
        server.sys,
        "argv",
        ["coding-agent-server", "--port", "0", "--token", "tok"],
    )

    server.main()

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["event"] == "coding_agent_server"
    assert payload["version"] == "0.1.0"
    assert _FakeServer.last is not None
    assert _FakeServer.last.ran is True
    assert _FakeServer.last.should_exit is True


def test_agent_server_main_requires_token(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("AGENT_INTERNAL_TOKEN", raising=False)
    monkeypatch.setattr(
        agent_server.sys, "argv", ["coding-agent-agent", "--port", "0"]
    )
    with pytest.raises(SystemExit) as exc:
        agent_server.main()
    assert exc.value.code == 2
    assert "AGENT_INTERNAL_TOKEN" in capsys.readouterr().err


def test_agent_server_main_reads_stdin_token(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    seen: dict[str, str] = {}
    real_create = agent_server.create_agent_app

    def create(settings, *, token="", runtime_factory=agent_server.AgentRuntime, **kwargs):
        seen["token"] = token
        return real_create(
            settings,
            token=token,
            runtime_factory=runtime_factory,
            **kwargs,
        )

    monkeypatch.setattr(agent_server, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(agent_server, "create_agent_app", create)
    monkeypatch.setattr(agent_server.uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(agent_server.sys, "stdin", io.StringIO("stdin-token\n"))
    monkeypatch.setattr(
        agent_server.sys,
        "argv",
        ["coding-agent-agent", "--port", "0", "--token-stdin"],
    )

    agent_server.main()

    assert seen["token"] == "stdin-token"
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["event"] == "coding_agent_agent_server"
    assert _FakeServer.last is not None
    assert _FakeServer.last.ran is True


def test_agent_server_reads_token_from_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "env-token")
    app = agent_server.create_agent_app(_settings(tmp_path))
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert (
        client.get(
            "/v1/meta", headers={"Authorization": "Bearer env-token"}
        ).status_code
        == 200
    )
