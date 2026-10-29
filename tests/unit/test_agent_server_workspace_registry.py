"""RFC-0001：A3 对工作区路径的最终校验。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coding_agent import agent_server
from coding_agent.agent_protocol import AgentContext
from coding_agent.config import Settings
from coding_agent.platform.paths import PathGuardError

ensure_host_workspace = agent_server.ensure_host_workspace

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


def _host_headers(workspace: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-Agent-Context": AgentContext(
            workspace=workspace,
            workspace_id="wid-host",
            workspace_kind="host",
            a2l_id="a2l-local",
            root_handle="h_abc",
        ).to_header(),
    }


class _FakeCaller:
    def call(self, *_args, **_kwargs) -> dict:
        return {}


def _host_app(tmp_path, monkeypatch, **kwargs) -> TestClient:
    """host 上下文要能构造 runtime，所以给 acceptor 打桩。

    本文件只关心路径校验，桩只需让 `caller_for` 返回非 None。
    """
    monkeypatch.setattr(
        agent_server.HostBrokerAcceptor,
        "caller_for",
        lambda _self, _broker_id: _FakeCaller(),
        raising=True,
    )
    settings = _settings(tmp_path, host_broker_enabled=True, **kwargs)
    return TestClient(
        agent_server.create_agent_app(
            settings, token=TOKEN, runtime_factory=_Runtime
        )
    )


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


def test_registry_accepts_host_path_inside_allowed_root(
    tmp_path, monkeypatch
) -> None:
    """host 工作区不再跳过 ensure_inside；Windows 形式的根也能对上。"""
    client = _host_app(
        tmp_path,
        monkeypatch,
        workspace_registry=True,
        workspace_root="D:\\ws",
    )
    response = client.get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_host_headers("D:\\ws\\demo"),
    )
    assert response.status_code == 200


def test_registry_rejects_host_path_outside_allowed_root(
    tmp_path, monkeypatch
) -> None:
    client = _host_app(
        tmp_path,
        monkeypatch,
        workspace_registry=True,
        workspace_root="D:\\ws",
    )
    response = client.get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_host_headers("D:\\other\\demo"),
    )
    assert response.status_code == 403


def test_registry_requires_root_for_host_too(tmp_path, monkeypatch) -> None:
    """注册表开着却没配根时，host 也 fail-closed。"""
    client = _host_app(
        tmp_path, monkeypatch, workspace_registry=True, workspace_root=""
    )
    response = client.get(
        "/v1/snapshots",
        params={"thread_id": "t1"},
        headers=_host_headers("D:\\ws\\demo"),
    )
    assert response.status_code == 400


def test_host_register_skips_workspace_root_check(
    tmp_path, monkeypatch
) -> None:
    """注册请求还没有目录：workspace 为空，不该被路径校验拦下。"""
    client = _host_app(
        tmp_path,
        monkeypatch,
        workspace_registry=True,
        workspace_root="D:\\ws",
    )
    response = client.post(
        "/v1/host-workspaces/register", headers=_host_headers("")
    )
    assert response.status_code == 200
    assert response.json()["a2l_id"] == "a2l-local"


def test_host_workspace_lexical_fallback() -> None:
    """注册表关闭时也要挡住 UNC/设备/相对路径（兜底，不是主防线）。"""
    ensure_host_workspace("")  # 还没选目录
    ensure_host_workspace("D:\\ws\\demo")
    ensure_host_workspace("C:/ws/demo")
    for bad in (
        "\\\\host\\share",
        "//host/share",
        "\\\\.\\PhysicalDrive0",
        "relative/demo",
        "D:\\ws\\\x00demo",
    ):
        with pytest.raises(PathGuardError):
            ensure_host_workspace(bad)


def test_agent_context_header_roundtrip_keeps_workspace_id() -> None:
    header = AgentContext(
        tenant="t", user="u", workspace="/srv/ws/x", workspace_id="wid"
    ).to_header()
    restored = AgentContext.from_header(header)
    assert restored.workspace_id == "wid"
    assert restored.workspace == "/srv/ws/x"
