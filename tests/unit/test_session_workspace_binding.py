"""RFC-0001：会话绑定 workspace_id 与旧路径兼容。"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import current_user
from coding_agent.web.models import SessionConfig
from coding_agent.web.routes.sessions import (
    SessionDeps,
    register_session_routes,
)

WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
ROOT = "/srv/ws/alice/alice/demo"


class _Runtimes:
    def contains(self, _key) -> bool:
        return False


def _deps(
    configs: dict,
    *,
    registry_enabled: bool,
    bind_row: dict | None = None,
) -> SessionDeps:
    store: dict[tuple[str, str], SessionConfig] = configs

    def config_for(_user, _tenant, thread_id, *, create=False):
        return store.get((_user.name, thread_id))

    def set_config(user, _tenant, config):
        store[(user.name, config.thread_id)] = config

    def bind_workspace(_user, workspace_id):
        if bind_row is None or workspace_id != bind_row["workspace_id"]:
            raise HTTPException(status_code=404, detail="工作区不存在")
        return bind_row

    tenant = SimpleNamespace(user="alice", workspace="/tmp/legacy")

    return SessionDeps(
        tenant=lambda _user: tenant,
        sweep_expired=_noop,
        load_configs=lambda *_args: None,
        config_for=config_for,
        set_config=set_config,
        persist_configs=lambda *_args: None,
        effective_access=lambda config: (
            config.access_mode if config else "read_only"
        ),
        capabilities=lambda _config: {},
        reject_deleted=lambda *_args: None,
        reject_active=lambda *_args: None,
        reject_db_degraded=lambda: None,
        rate_limit=lambda _user: None,
        resolve_workspace=lambda _name, ws: ws or "/tmp/legacy",
        bind_workspace=bind_workspace,
        registry_enabled=registry_enabled,
        validate_provider=lambda *_args: None,
        agent_json=_noop,
        ops_runtime=lambda *_args: None,
        agent_client=None,
        allow_write=False,
        registry_lock=threading.Lock(),
        session_configs=store,
        deleted_sessions=set(),
        runtimes=_Runtimes(),
    )


async def _noop(*_args, **_kwargs):
    return None


def _client(deps: SessionDeps) -> TestClient:
    app = FastAPI()
    register_session_routes(app, deps)
    app.dependency_overrides[current_user] = lambda: AuthUser(name="alice")
    return TestClient(app)


def test_bind_registered_workspace() -> None:
    configs: dict = {}
    row = {
        "workspace_id": WORKSPACE_ID,
        "root_path": ROOT,
        "status": "active",
    }
    client = _client(
        _deps(configs, registry_enabled=True, bind_row=row)
    )
    response = client.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace_id": WORKSPACE_ID},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"] == ROOT
    assert payload["workspace_id"] == WORKSPACE_ID
    assert configs[("alice", "t1")].workspace_id == WORKSPACE_ID


def test_bind_registered_host_workspace_keeps_handle_fields() -> None:
    configs: dict = {}
    row = {
        "workspace_id": WORKSPACE_ID,
        "root_path": "",
        "kind": "host",
        "a2l_id": "a2l-1",
        "root_handle": "h-1",
        "status": "active",
    }
    client = _client(
        _deps(configs, registry_enabled=True, bind_row=row)
    )
    response = client.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace_id": WORKSPACE_ID},
    )
    assert response.status_code == 200
    config = configs[("alice", "t1")]
    assert config.workspace_kind == "host"
    assert config.a2l_id == "a2l-1"
    assert config.root_handle == "h-1"


def test_registry_requires_workspace_id() -> None:
    client = _client(_deps({}, registry_enabled=True))
    response = client.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace": "/tmp/whatever"},
    )
    assert response.status_code == 400


def test_unknown_workspace_is_404() -> None:
    client = _client(_deps({}, registry_enabled=True, bind_row=None))
    response = client.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace_id": WORKSPACE_ID},
    )
    assert response.status_code == 404


def test_binding_is_immutable() -> None:
    configs = {
        ("alice", "t1"): SessionConfig(
            thread_id="t1",
            workspace=ROOT,
            workspace_id=WORKSPACE_ID,
        )
    }
    client = _client(
        _deps(configs, registry_enabled=True, bind_row=None)
    )
    response = client.post(
        "/api/sessions",
        json={
            "thread_id": "t1",
            "workspace_id": "22222222-2222-2222-2222-222222222222",
        },
    )
    assert response.status_code == 409


def test_registry_off_keeps_legacy_workspace_path() -> None:
    configs: dict = {}
    client = _client(_deps(configs, registry_enabled=False))
    response = client.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace": "/tmp/legacy"},
    )
    assert response.status_code == 200
    assert response.json()["workspace"] == "/tmp/legacy"
    assert response.json()["workspace_id"] == ""
    assert configs[("alice", "t1")].workspace_id == ""


def test_registry_off_ignores_workspace_id() -> None:
    configs: dict = {}
    client = _client(_deps(configs, registry_enabled=False))
    response = client.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace_id": WORKSPACE_ID},
    )
    assert response.status_code == 200
    assert response.json()["workspace_id"] == ""
