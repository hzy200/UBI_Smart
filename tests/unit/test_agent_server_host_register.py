"""A3 本机工作区注册路由：只回传 handle，不接收绝对路径。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ubismart.agent_protocol import AgentContext
from ubismart.config import Settings
from ubismart.servers.agent_server import create_agent_app


class FakeCaller:
    def call(self, kind, *, workspace, payload, grant_id=""):
        assert kind == "host.workspace.register"
        assert workspace == {}
        return {
            "workspace_id": "ws-1",
            "root_handle": "h-1",
            "display_name": "demo",
            "a2l_id": "a2l-1",
        }


class FakeAcceptor:
    def caller_for(self, a2l_id):
        return FakeCaller() if a2l_id == "a2l-1" else None


def test_host_workspace_register_route(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        AGENT_AGENT_DATA_ROOT=str(tmp_path),
        AGENT_HOST_BROKER_ENABLED=False,
    )
    app = create_agent_app(settings, token="t")
    app.state.host_broker_acceptor = FakeAcceptor()
    context = AgentContext(
        tenant="t",
        user="u",
        workspace_kind="host",
        a2l_id="a2l-1",
    )
    headers = {
        "Authorization": "Bearer t",
        "X-Agent-Context": context.to_header(),
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/host-workspaces/register", headers=headers
        )
    assert response.status_code == 200
    data = response.json()
    assert data["workspace_id"] == "ws-1"
    assert data["root_handle"] == "h-1"
    assert data["a2l_id"] == "a2l-1"


def test_host_workspace_register_route_requires_a2l(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        AGENT_AGENT_DATA_ROOT=str(tmp_path),
        AGENT_HOST_BROKER_ENABLED=False,
    )
    app = create_agent_app(settings, token="t")
    context = AgentContext(
        tenant="t", user="u", workspace_kind="host", a2l_id="a2l-1"
    )
    headers = {
        "Authorization": "Bearer t",
        "X-Agent-Context": context.to_header(),
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/host-workspaces/register", headers=headers
        )
    assert response.status_code == 503
