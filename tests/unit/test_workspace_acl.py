"""P1-3：工作区只读 ACL。

边界：授权只让成员“看得到”元数据与 revision，不能改、不能把工作区
绑进自己的会话执行（执行绑定仍要求 owner）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from coding_agent.config import Settings
from coding_agent.control.schema import Base, ControlUser
from coding_agent.control.workspaces import (
    WorkspaceNotFound,
    WorkspacePolicy,
    WorkspaceStore,
)
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import current_user
from coding_agent.web.routes.workspaces import (
    WorkspaceRegistryDeps,
    register_workspace_registry_routes,
)


@pytest.fixture()
def store(tmp_path) -> WorkspaceStore:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}", future=True
    )
    Base.metadata.create_all(engine)
    return WorkspaceStore(
        engine,
        WorkspacePolicy(root="/srv/ws", mount_roots=("/mnt/d",)),
    )


def _account(store: WorkspaceStore, username: str, tenant="default") -> None:
    with Session(store.engine) as session, session.begin():
        session.add(
            ControlUser(
                id=f"u-{username}",
                tenant=tenant,
                username=username,
                role="member",
                status="active",
                created_at="2026-10-28",
                updated_at="2026-10-28",
            )
        )


def _route_client(
    store: WorkspaceStore, user: AuthUser
) -> TestClient:
    app = FastAPI()
    register_workspace_registry_routes(
        app,
        WorkspaceRegistryDeps(
            rate_limit=lambda _user: None,
            store=store,
            enabled=True,
            settings=Settings(
                _env_file=None, workspace_mount_roots="/mnt/d"
            ),
        ),
    )
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def test_grant_makes_metadata_visible_without_paths(store) -> None:
    _account(store, "alice")
    _account(store, "bob")
    created = store.create("alice", "alice", "Shared")
    assert store.list_visible("default", "bob", "member") == []

    grant = store.acl_grant(
        "default", created["workspace_id"], "bob", granted_by="admin"
    )
    assert grant["permission"] == "read"
    assert grant["grantee"] == "bob"

    rows = store.list_visible("default", "bob", "member")
    assert len(rows) == 1
    assert rows[0]["workspace_id"] == created["workspace_id"]
    assert rows[0]["acl_role"] == "read"
    # 只读共享不暴露宿主绝对路径/A2L handle。
    assert "root_path" not in rows[0]
    assert "root_handle" not in rows[0]

    visible = store.get_visible(
        "default", "bob", "member", created["workspace_id"]
    )
    assert visible is not None
    assert visible["acl_role"] == "read"

    assert store.acl_revoke(
        "default", created["workspace_id"], "bob"
    ) is True
    assert store.list_visible("default", "bob", "member") == []


def test_grant_is_idempotent_and_requires_workspace(store) -> None:
    _account(store, "alice")
    _account(store, "bob")
    created = store.create("alice", "alice", "Shared")
    first = store.acl_grant(
        "default", created["workspace_id"], "bob", granted_by="admin"
    )
    again = store.acl_grant(
        "default", created["workspace_id"], "bob", granted_by="admin"
    )
    assert first["id"] == again["id"]
    assert len(store.acl_list("default", created["workspace_id"])) == 1

    with pytest.raises(WorkspaceNotFound):
        store.acl_grant("default", "missing", "bob")


def test_admin_and_auditor_see_tenant_workspaces(store) -> None:
    _account(store, "alice")
    created = store.create("alice", "alice", "Alice Project")

    admin_rows = store.list_visible("default", "root", "admin")
    assert [item["workspace_id"] for item in admin_rows] == [
        created["workspace_id"]
    ]
    assert admin_rows[0]["acl_role"] == "admin"

    auditor_rows = store.list_visible("default", "auditor", "auditor")
    assert [item["workspace_id"] for item in auditor_rows] == [
        created["workspace_id"]
    ]
    assert auditor_rows[0]["acl_role"] == "auditor"

    # 其他成员没有授权就看不到。
    assert store.list_visible("default", "carol", "member") == []


def test_auditor_list_does_not_create_default_workspace(store) -> None:
    """审计 GET 不应触发隐式写：默认 host 工作区留给真正运行的人。"""
    settings = Settings(
        _env_file=None,
        workspace_kind_default="host",
        host_root_handle="handle-1",
        host_workspace_id="host-ws-1",
        host_a2l_id="a2l-local",
    )
    app = FastAPI()
    register_workspace_registry_routes(
        app,
        WorkspaceRegistryDeps(
            rate_limit=lambda _user: None,
            store=store,
            enabled=True,
            settings=settings,
        ),
    )
    app.dependency_overrides[current_user] = lambda: AuthUser(
        name="audit", tenant="default", role="auditor"
    )
    client = TestClient(app)

    assert client.get("/api/workspaces").json()["workspaces"] == []
    assert store.list("audit") == []


def test_grantee_can_read_but_not_modify_workspace(store) -> None:
    _account(store, "alice")
    _account(store, "bob")
    created = store.create("alice", "alice", "Shared")
    store.acl_grant(
        "default", created["workspace_id"], "bob", granted_by="admin"
    )
    client = _route_client(
        store,
        AuthUser(name="bob", tenant="default", role="member"),
    )
    workspace_id = created["workspace_id"]

    listing = client.get("/api/workspaces")
    assert listing.status_code == 200
    rows = listing.json()["workspaces"]
    assert [row["workspace_id"] for row in rows] == [workspace_id]
    assert rows[0]["acl_role"] == "read"
    assert "root_path" not in rows[0]

    assert client.get(f"/api/workspaces/{workspace_id}").status_code == 200
    # 写路径仍只认 owner。
    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 404
    assert (
        client.patch(
            f"/api/workspaces/{workspace_id}",
            json={"display_name": "Hijacked"},
        ).status_code
        == 404
    )
