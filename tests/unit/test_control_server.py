"""A2 控制面进程入口：参数、迁移、降级与 shutdown 路径。"""

from __future__ import annotations

import io
import json
import sys
import types
from typing import Any

import pytest
import uvicorn

from ubismart.servers import control_server


class _Settings:
    agent_internal_url = ""
    agent_internal_token = ""
    agent_internal_hmac_key = ""
    agent_internal_hmac_required = False
    control_db_url = ""
    resolved_audit_dir = "/tmp/audit"
    web_accounts_enabled = False
    web_cookie_secure = False
    drain_timeout_s = 60


class _FakeAuth:
    def __init__(
        self,
        *,
        tokens: dict[str, str] | None = None,
        allow_anonymous_local: bool = True,
        enabled: bool = True,
    ) -> None:
        self.tokens = tokens or {}
        self.allow_anonymous_local = allow_anonymous_local
        self.enabled = enabled

    @classmethod
    def from_settings(cls, settings: Any) -> _FakeAuth:
        return cls(enabled=bool(getattr(settings, "auth_enabled", False)))


class _FakeMeta:
    def to_dict(self) -> dict[str, Any]:
        return {"name": "agent", "version": "1"}


class _FakeCompat:
    def __init__(self, ok: bool, reason: str = "", warning: str = "") -> None:
        self.ok = ok
        self.reason = reason
        self.warning = warning


class _FakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.raise_on_probe: Exception | None = None

    async def probe(self) -> _FakeMeta:
        if self.raise_on_probe is not None:
            raise self.raise_on_probe
        return _FakeMeta()


class _FakeUvicornConfig:
    def __init__(self, app: Any, **kwargs: Any) -> None:
        self.app = app
        self.kwargs = kwargs


class _FakeServer:
    instances: list[_FakeServer] = []

    def __init__(self, config: _FakeUvicornConfig) -> None:
        self.config = config
        self.should_exit = False
        self.ran = False
        _FakeServer.instances.append(self)

    def run(self) -> None:
        self.ran = True


@pytest.fixture
def patched(monkeypatch):
    _FakeServer.instances.clear()
    monkeypatch.setattr(control_server, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        control_server, "_handshake", lambda settings: (None, {"state": "in_process"})
    )
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    created: list[dict[str, Any]] = []

    def fake_create_app(settings, auth, **kwargs):
        created.append({"auth": auth, **kwargs})
        return app

    monkeypatch.setattr("ubismart.web.create_app", fake_create_app)
    monkeypatch.setattr("ubismart.web.auth.AuthConfig", _FakeAuth)
    monkeypatch.setattr(uvicorn, "Config", _FakeUvicornConfig)
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    return types.SimpleNamespace(app=app, created=created, monkeypatch=monkeypatch)


def _main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["control_server", *argv])
    return control_server.main()


def test_handshake_in_process_without_internal_url() -> None:
    client, status = control_server._handshake(_Settings())
    assert client is None
    assert status == {"state": "in_process"}


def test_handshake_reports_unavailable_on_probe_error(monkeypatch) -> None:
    settings = _Settings()
    settings.agent_internal_url = "http://127.0.0.1:1"

    def fake_client(*args, **kwargs):
        client = _FakeClient(*args, **kwargs)
        client.raise_on_probe = TimeoutError("no answer")
        return client

    monkeypatch.setattr("ubismart.control.agent_client.HttpAgentClient", fake_client)
    client, status = control_server._handshake(settings)
    assert client is not None
    assert status["state"] == "unavailable"
    assert "TimeoutError" in status["error"]


@pytest.mark.parametrize("ok", [True, False])
def test_handshake_reports_compatibility(monkeypatch, ok: bool) -> None:
    settings = _Settings()
    settings.agent_internal_url = "http://127.0.0.1:9"
    monkeypatch.setattr(
        "ubismart.control.agent_client.HttpAgentClient", _FakeClient
    )
    monkeypatch.setattr(
        "ubismart.agent_protocol.check_compatibility",
        lambda meta: _FakeCompat(ok, reason="same" if ok else "mismatch"),
    )
    _, status = control_server._handshake(settings)
    assert status["state"] == ("ok" if ok else "incompatible")
    assert status["meta"] == {"name": "agent", "version": "1"}
    assert status["reason"] == ("same" if ok else "mismatch")


def test_migrate_skips_when_url_missing(capsys) -> None:
    assert control_server._migrate_control_db(_Settings()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "control_db_migrate_skipped"


def test_migrate_failure_returns_one(monkeypatch, capsys) -> None:
    settings = _Settings()
    settings.control_db_url = "sqlite:///x.db"

    def boom(*args, **kwargs):
        raise RuntimeError("migration broke")

    monkeypatch.setattr("ubismart.control.migrate.upgrade", boom)
    assert control_server._migrate_control_db(settings) == 1
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert json.loads(captured.err)["event"] == "control_db_migrate_failed"


def test_migrate_success_returns_zero(monkeypatch, capsys) -> None:
    settings = _Settings()
    settings.control_db_url = "sqlite:///x.db"
    seen: list[dict[str, Any]] = []

    def fake_upgrade(url, *, backup, backup_dir):
        seen.append({"url": url, "backup": backup, "backup_dir": str(backup_dir)})
        return {"revision": "head"}

    monkeypatch.setattr("ubismart.control.migrate.upgrade", fake_upgrade)
    assert control_server._migrate_control_db(settings) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "control_db_migrated"
    assert payload["revision"] == "head"
    assert seen[0]["backup"] is True


def test_migrate_only_stops_before_http(patched, monkeypatch) -> None:
    monkeypatch.setattr(control_server, "_migrate_control_db", lambda settings: 0)
    assert _main(monkeypatch, "--migrate-only") == 0
    assert _FakeServer.instances == []


def test_migrate_only_propagates_failure(patched, monkeypatch) -> None:
    monkeypatch.setattr(control_server, "_migrate_control_db", lambda settings: 1)
    assert _main(monkeypatch, "--migrate-only") == 1
    assert _FakeServer.instances == []


def test_desktop_requires_token_stdin(patched, monkeypatch) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _main(monkeypatch, "--desktop")
    assert excinfo.value.code == 2


def test_desktop_and_multi_user_conflict(patched, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok\n"))
    with pytest.raises(SystemExit) as excinfo:
        _main(monkeypatch, "--desktop", "--token-stdin", "--multi-user")
    assert excinfo.value.code == 2


def test_multi_user_requires_auth(patched, monkeypatch) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _main(monkeypatch, "--multi-user")
    assert excinfo.value.code == 2


def test_non_loopback_requires_auth(patched, monkeypatch) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _main(monkeypatch, "--host", "0.0.0.0")
    assert excinfo.value.code == 2


def test_remote_internal_url_requires_context_hmac(patched, monkeypatch) -> None:
    """A2→A3 跨机时 context 必须验签：只靠内部 token 等于把 tenant 交给中间人。"""
    settings = _Settings()
    assert control_server._internal_url_is_remote(settings) is False
    settings.agent_internal_url = "http://127.0.0.1:8790"
    assert control_server._internal_url_is_remote(settings) is False
    settings.agent_internal_url = "http://10.0.0.5:8790"
    assert control_server._internal_url_is_remote(settings) is True

    monkeypatch.setattr(control_server, "get_settings", lambda: settings)
    with pytest.raises(SystemExit) as excinfo:
        _main(monkeypatch)
    assert excinfo.value.code == 2

    # 配了 key 就放行（后续启动流程仍由 patched fixture 接管）。
    settings.agent_internal_hmac_key = "k"
    assert _main(monkeypatch) == 0


def test_loopback_start_installs_shutdown(patched, monkeypatch, capsys) -> None:
    code = _main(
        monkeypatch,
        "--port",
        "8781",
        "--log-level",
        "info",
        "--ssl-keyfile",
        "/k.pem",
        "--ssl-certfile",
        "/c.pem",
    )
    assert code == 0
    server = _FakeServer.instances[-1]
    assert server.ran is True
    assert server.config.kwargs["port"] == 8781
    assert server.config.kwargs["ssl_keyfile"] == "/k.pem"
    assert server.config.kwargs["ssl_certfile"] == "/c.pem"
    patched.app.state.shutdown()
    assert server.should_exit is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "coding_agent_control_plane"
    assert payload["agent_state"] == "in_process"
    assert patched.created[-1]["client_source"] == "web"


def test_non_loopback_accounts_require_secure_cookie(
    patched, monkeypatch
) -> None:
    settings = _Settings()
    settings.web_accounts_enabled = True
    settings.web_cookie_secure = False
    monkeypatch.setattr(control_server, "get_settings", lambda: settings)
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok\n"))
    with pytest.raises(SystemExit) as excinfo:
        _main(monkeypatch, "--host", "0.0.0.0", "--token-stdin")
    assert excinfo.value.code == 2

    settings.web_cookie_secure = True
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok\n"))
    assert _main(monkeypatch, "--host", "0.0.0.0", "--token-stdin") == 0


def test_token_stdin_builds_local_auth(patched, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("secret-token\n"))
    assert _main(monkeypatch, "--token-stdin") == 0
    auth = patched.created[-1]["auth"]
    assert auth.tokens == {"secret-token": "local"}
    assert auth.allow_anonymous_local is False


def test_desktop_mode_disables_multi_user(patched, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("tok\n"))
    assert _main(monkeypatch, "--desktop", "--token-stdin") == 0
    created = patched.created[-1]
    assert created["multi_user"] is False
    assert created["desktop_mode"] is True
    assert created["client_source"] == "desktop"


def test_handshake_failure_is_reported_but_server_starts(
    patched, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        control_server,
        "_handshake",
        lambda settings: (None, {"state": "unavailable", "error": "down"}),
    )
    assert _main(monkeypatch) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.err)["event"] == "agent_handshake_failed"
    assert patched.app.state.agent_status["state"] == "unavailable"
