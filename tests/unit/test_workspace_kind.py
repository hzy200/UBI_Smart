"""host 工作区只存 handle，不在 A2 保存绝对路径。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from ubismart.control.schema import Base
from ubismart.control.workspaces import (
    WorkspacePathError,
    WorkspacePolicy,
    WorkspaceStore,
)


def _store(tmp_path) -> WorkspaceStore:
    engine = create_engine(f"sqlite:///{tmp_path / 'control.db'}", future=True)
    Base.metadata.create_all(engine)
    return WorkspaceStore(
        engine,
        WorkspacePolicy(root="/srv/ws", archive_root="/srv/archive"),
    )


def test_host_workspace_stores_handle_not_path(tmp_path) -> None:
    store = _store(tmp_path)
    created = store.create(
        "tenant",
        "alice",
        "Host Project",
        source="local_mount",
        kind="host",
        a2l_id="a2l-1",
        root_handle="h_abc",
        status="active",
    )
    assert created["kind"] == "host"
    assert created["a2l_id"] == "a2l-1"
    assert created["root_handle"] == "h_abc"
    assert created["root_path"] == ""
    assert created["root_kind"] == "ntfs"
    assert store.get(created["workspace_id"])["kind"] == "host"


def test_host_workspace_requires_handle_and_rejects_path(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(WorkspacePathError):
        store.create(
            "tenant", "alice", "Host", source="local_mount", kind="host"
        )
    with pytest.raises(WorkspacePathError):
        store.create(
            "tenant",
            "alice",
            "Host",
            source="local_mount",
            kind="host",
            root_handle="h_1",
            root_path="/tmp/x",
        )


def test_sandbox_default_unchanged(tmp_path) -> None:
    store = _store(tmp_path)
    created = store.create("tenant", "alice", "Sandbox")
    assert created["kind"] == "sandbox"
    assert created["root_path"].startswith("/srv/ws/")
    assert created["root_handle"] == ""
