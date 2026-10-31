"""会话上下文：host 默认值不能覆盖旧 sandbox 会话。"""

from __future__ import annotations

import threading

from ubismart.config import Settings
from ubismart.identity.tenancy import WebTenant
from ubismart.web.auth import AuthUser
from ubismart.web.models import SessionConfig
from ubismart.web.services import (
    AgentGateway,
    ProviderService,
    SessionCatalog,
)


def _gateway() -> AgentGateway:
    settings = Settings(
        _env_file=None,
        workspace_kind_default="host",
        host_workspace_id="ws-smoke",
        host_root_handle="h-smoke",
        host_a2l_id="a2l-local",
        deepseek_api_key="sk-test",
    )
    providers = ProviderService(
        settings, base_llm_providers="", is_multi_user=False
    )
    return AgentGateway(
        agent_client=None,
        providers=providers,
        effective_access=lambda config: (
            config.access_mode if config else "read_only"
        ),
        allow_write=False,
    )


def _tenant(gateway: AgentGateway) -> WebTenant:
    return WebTenant(
        user="local",
        multi_user=False,
        workspace="/home/agent/agent-ws",
        settings=gateway.providers.settings,
    )


def _user() -> AuthUser:
    return AuthUser(name="local")


def test_legacy_sandbox_session_keeps_its_workspace() -> None:
    gateway = _gateway()
    config = SessionConfig(
        thread_id="t1",
        workspace="/home/agent/agent-ws",
        workspace_id="",
        workspace_kind="sandbox",
    )
    context = gateway.context(
        _user(), _tenant(gateway), config, include_keys=False
    )
    assert context.workspace == "/home/agent/agent-ws"
    assert context.workspace_kind == "sandbox"
    assert context.workspace_id == ""
    assert context.root_handle == ""


def test_registered_host_session_uses_its_own_fields() -> None:
    gateway = _gateway()
    config = SessionConfig(
        thread_id="t2",
        workspace="",
        workspace_id="ws_web",
        workspace_kind="host",
        a2l_id="a2l-local",
        root_handle="h_web",
    )
    context = gateway.context(
        _user(), _tenant(gateway), config, include_keys=False
    )
    assert context.workspace_kind == "host"
    assert context.workspace_id == "ws_web"
    assert context.root_handle == "h_web"


def test_no_session_still_gets_host_default() -> None:
    gateway = _gateway()
    context = gateway.context(
        _user(), _tenant(gateway), None, include_keys=False
    )
    assert context.workspace_kind == "host"
    assert context.workspace_id == "ws-smoke"
    assert context.root_handle == "h-smoke"


class _Store:
    def __init__(self) -> None:
        self.saved: list[dict] | None = None

    def get_workspace(self, workspace_id: str) -> dict:
        assert workspace_id == "ws_d3"
        return {
            "workspace_id": "ws_d3",
            "kind": "host",
            "a2l_id": "a2l-local",
            "root_handle": "h_web",
            "root_path": "",
            "display_name": "web",
        }

    def save_sessions(self, _tenant, sessions, _deleted) -> None:
        self.saved = sessions


def test_catalog_repairs_host_binding_from_workspace_row() -> None:
    gateway = _gateway()
    tenant = _tenant(gateway)
    store = _Store()
    catalog = SessionCatalog(
        tenants=object(),
        tenant=lambda _user: tenant,
        default_access="read_only",
        control_store=store,
        control_db_url="sqlite:///unused.db",
        session_configs={},
        deleted_sessions=set(),
        sessions_loaded=set(),
        registry_lock=threading.Lock(),
        session_file_lock=threading.Lock(),
        db_status=lambda: {"state": "ok"},
        mark_db_degraded=lambda _exc: None,
    )
    catalog.install_rows(
        _user(),
        tenant,
        [
            {
                "thread_id": "t1",
                "workspace": "/home/agent/agent-ws",
                "workspace_id": "ws_d3",
            }
        ],
    )
    config = catalog.session_configs[("local", "t1")]
    assert config.workspace_kind == "host"
    assert config.workspace == ""
    assert config.a2l_id == "a2l-local"
    assert config.root_handle == "h_web"
    # 修复结果要落库，重启后不依赖二次解析。
    assert store.saved is not None
    row = next(item for item in store.saved if item["thread_id"] == "t1")
    assert row["workspace_kind"] == "host"
    assert row["root_handle"] == "h_web"
