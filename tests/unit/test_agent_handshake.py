"""Phase D1：A2 ↔ A3 版本握手与能力元信息。"""

from __future__ import annotations

import asyncio
import socket

import httpx
from fastapi.testclient import TestClient

from ubismart import AGENT_API_VERSION, __version__
from ubismart.config import Settings
from ubismart.control.agent_client import HttpAgentClient
from ubismart.control.protocol import (
    AGENT_CAPABILITIES,
    AgentMeta,
    check_compatibility,
)
from ubismart.servers import agent_server, control_server


def _settings(tmp_path, **updates) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        agent_data_root=str(tmp_path / "agent"),
        **updates,
    )


def _meta(api_version: int = AGENT_API_VERSION, version: str = __version__):
    return {
        "service": "agent-server",
        "version": version,
        "api_version": api_version,
        "execution_backend": "local",
        "capabilities": {"run_lifecycle": ["runs", "stream"]},
    }


def test_agent_server_healthz_reports_version(tmp_path) -> None:
    app = agent_server.create_agent_app(_settings(tmp_path), token="tok")
    body = TestClient(app).get("/healthz").json()
    assert body == {
        "status": "ok",
        "service": "agent-server",
        "version": __version__,
        "api_version": AGENT_API_VERSION,
    }


def test_agent_server_meta_requires_token(tmp_path) -> None:
    client = TestClient(
        agent_server.create_agent_app(_settings(tmp_path), token="tok")
    )
    assert client.get("/v1/meta").status_code == 401
    body = client.get(
        "/v1/meta", headers={"Authorization": "Bearer tok"}
    ).json()
    assert body["api_version"] == AGENT_API_VERSION
    assert body["version"] == __version__
    assert body["capabilities"] == {
        group: list(items) for group, items in AGENT_CAPABILITIES.items()
    }


def test_http_agent_client_probe_validates_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/meta"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=_meta())

    client = HttpAgentClient(
        "http://agent", "tok", transport=httpx.MockTransport(handler)
    )
    meta = asyncio.run(client.probe())
    assert meta.api_version == AGENT_API_VERSION
    assert meta.has("run_lifecycle", "runs")
    assert not meta.has("run_lifecycle", "missing")


def test_compatibility_rules() -> None:
    same = check_compatibility(AgentMeta.from_dict(_meta()))
    assert same.ok and not same.warning

    older = check_compatibility(
        AgentMeta.from_dict(_meta(api_version=AGENT_API_VERSION - 1))
    )
    assert not older.ok and "协议版本" in older.reason

    drift = check_compatibility(
        AgentMeta.from_dict(_meta(version="0.0.9"))
    )
    assert drift.ok and "版本" in drift.warning


def test_control_healthz_exposes_agent_state(tmp_path) -> None:
    from ubismart import web

    app = web.create_app(_settings(tmp_path))
    client = TestClient(app)
    assert client.get("/healthz").json()["agent_state"] == "in_process"
    app.state.agent_status = {"state": "unavailable", "error": "x"}
    assert client.get("/healthz").json()["agent_state"] == "unavailable"


def test_handshake_is_in_process_without_agent_url(tmp_path) -> None:
    client, status = control_server._handshake(_settings(tmp_path))
    assert client is None
    assert status == {"state": "in_process"}


def test_handshake_reports_unavailable_agent(tmp_path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = _settings(
        tmp_path,
        agent_internal_url=f"http://127.0.0.1:{port}",
        agent_internal_token="tok",
    )
    client, status = control_server._handshake(settings)
    assert client is not None
    assert status["state"] == "unavailable"
    assert status["error"]
    asyncio.run(client.aclose())


def test_handshake_reports_incompatible_agent(tmp_path, monkeypatch) -> None:
    async def fake_probe(self):
        return AgentMeta.from_dict(_meta(api_version=999))

    monkeypatch.setattr(HttpAgentClient, "probe", fake_probe)
    settings = _settings(
        tmp_path,
        agent_internal_url="http://127.0.0.1:9",
        agent_internal_token="tok",
    )
    _, status = control_server._handshake(settings)
    assert status["state"] == "incompatible"
    assert status["reason"]
