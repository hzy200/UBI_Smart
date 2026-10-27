"""RFC-0001：A3 对工作区路径的最终校验。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from coding_agent import agent_server
from coding_agent.agent_protocol import AgentContext
from coding_agent.config import Settings

TOKEN = "tok"


class _Runtime:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def list_snapshots(self, limit: int = 20):
        return []

    async def history(self, _thread_id, *, limit=None, offset=0):
        return []

    async def aclose(self) -> None:
        return None


def _settings(tmp_path, **kwargs) -> Settings:
    base = Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/srv/ws",
        agent_data_root=str(tmp_path / "agent"),
    )
    return base.model_copy(update=kwargs)


def _app(tmp_path, **kwargs) -> TestClient:
    settings = _settings(tmp_path, **kwargs)
    return TestClient(
        agent_server.create_agent_app(
            settings, token=TOKEN, runtime_factory=_Runtime
        )
    )


def _headers(
    workspace: str, workspace_id: str = ""
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-Agent-Context": AgentContext(
            workspace=workspace, workspace_id=workspace_id
        ).to_header(),
    }


def test_registry_accepts_path_inside_allowed_root(tmp_path) -> None:
    client = _app(
        tmp_path,
        workspace_registry=True,
        workspace_root="/srv/ws",
    )
    response = client.get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_headers("/srv/ws/alice/alice/demo", "wid-1"),
    )
    assert response.status_code == 200


def test_registry_rejects_path_outside_allowed_root(tmp_path) -> None:
    client = _app(
        tmp_path,
        workspace_registry=True,
        workspace_root="/srv/ws",
    )
    response = client.get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_headers("/etc"),
    )
    assert response.status_code == 403


def test_registry_requires_configured_root(tmp_path) -> None:
    client = _app(tmp_path, workspace_registry=True, workspace_root="")
    response = client.get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_headers("/srv/ws/alice/alice/demo"),
    )
    assert response.status_code == 400


def test_history_ignores_workspace_root(tmp_path) -> None:
    """旧会话的工作区可能不在新注册表根内，但历史仍要能看。"""
    client = _app(
        tmp_path,
        workspace_registry=True,
        workspace_root="/srv/ws",
    )
    response = client.get(
        "/v1/history/t1",
        headers=_headers("/home/agent/agent-ws"),
    )
    assert response.status_code == 200
    assert response.json() == {"messages": []}


def test_registry_off_keeps_legacy_behavior(tmp_path) -> None:
    client = _app(tmp_path, workspace_registry=False)
    response = client.get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_headers("/srv/ws/alice/alice/demo"),
    )
    assert response.status_code == 200


def test_agent_context_header_roundtrip_keeps_workspace_id() -> None:
    header = AgentContext(
        tenant="t", user="u", workspace="/srv/ws/x", workspace_id="wid"
    ).to_header()
    restored = AgentContext.from_header(header)
    assert restored.workspace_id == "wid"
    assert restored.workspace == "/srv/ws/x"
