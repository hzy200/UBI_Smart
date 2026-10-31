"""RFC-0001：旧 control_sessions.workspace 的幂等回填。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ubismart.control.schema import Base, ControlSession
from ubismart.control.workspaces import (
    WorkspacePolicy,
    WorkspaceStore,
)


@pytest.fixture()
def engine(tmp_path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'control.db'}", future=True)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def store(engine: Engine) -> WorkspaceStore:
    return WorkspaceStore(
        engine,
        WorkspacePolicy(root="/srv/ws", mount_roots=("/mnt/d",)),
    )


def _session(
    engine: Engine,
    *,
    tenant: str = "t1",
    thread_id: str = "t1",
    workspace: str = "/srv/ws/t1/alice/proj",
    title: str = "Project",
) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            ControlSession(
                tenant=tenant,
                thread_id=thread_id,
                workspace=workspace,
                title=title,
            )
        )


def test_dry_run_does_not_write(
    engine: Engine, store: WorkspaceStore
) -> None:
    _session(engine)
    result = store.migrate_legacy_sessions(dry_run=True)
    assert result == {
        "scanned": 1,
        "created": 1,
        "reused": 0,
        "skipped": 0,
        "dry_run": True,
    }
    assert store.list("t1", include_deleted=True) == []
    with Session(engine) as session:
        row = session.scalars(select(ControlSession)).one()
        assert row.workspace_id == ""


def test_migration_creates_legacy_workspace_and_fills_id(
    engine: Engine, store: WorkspaceStore
) -> None:
    _session(engine, thread_id="alice:t1")
    result = store.migrate_legacy_sessions(dry_run=False)
    assert result["created"] == 1

    rows = store.list("t1", include_deleted=True)
    assert len(rows) == 1
    assert rows[0]["source"] == "legacy"
    assert rows[0]["legacy"] is True
    assert rows[0]["owner"] == "alice"
    assert rows[0]["status"] == "active"
    assert rows[0]["root_path"] == "/srv/ws/t1/alice/proj"

    with Session(engine) as session:
        row = session.scalars(select(ControlSession)).one()
        assert row.workspace_id == rows[0]["workspace_id"]


def test_plain_thread_id_uses_local_owner(
    engine: Engine, store: WorkspaceStore
) -> None:
    _session(engine, thread_id="plain")
    store.migrate_legacy_sessions(dry_run=False)
    rows = store.list("t1", include_deleted=True)
    assert rows[0]["owner"] == "local"


def test_migration_is_idempotent_after_reset(
    engine: Engine, store: WorkspaceStore
) -> None:
    _session(engine)
    store.migrate_legacy_sessions(dry_run=False)
    first = store.list("t1", include_deleted=True)

    # 模拟“回填被重复执行”：清空 workspace_id 后再跑一次。
    with Session(engine) as session, session.begin():
        row = session.scalars(select(ControlSession)).one()
        row.workspace_id = ""

    result = store.migrate_legacy_sessions(dry_run=False)
    assert result["reused"] == 1
    assert result["created"] == 0
    second = store.list("t1", include_deleted=True)
    assert len(second) == 1
    assert second[0]["workspace_id"] == first[0]["workspace_id"]


def test_empty_workspace_is_skipped(
    engine: Engine, store: WorkspaceStore
) -> None:
    _session(engine, workspace="", title="No workspace")
    result = store.migrate_legacy_sessions(dry_run=False)
    assert result["skipped"] == 1
    assert result["created"] == 0


def test_invalid_path_is_skipped(
    engine: Engine, store: WorkspaceStore
) -> None:
    _session(engine, workspace="relative/path")
    result = store.migrate_legacy_sessions(dry_run=False)
    assert result["skipped"] == 1
    assert result["created"] == 0


def test_mount_path_marks_degraded(
    engine: Engine, store: WorkspaceStore
) -> None:
    _session(engine, workspace="/mnt/d/project")
    store.migrate_legacy_sessions(dry_run=False)
    rows = store.list("t1", include_deleted=True)
    assert rows[0]["root_kind"] == "mount"
    assert rows[0]["degraded_fs"] is True


def test_tenant_filter(engine: Engine, store: WorkspaceStore) -> None:
    _session(engine, tenant="t1", thread_id="a")
    _session(engine, tenant="t2", thread_id="b")
    result = store.migrate_legacy_sessions(tenant="t1", dry_run=False)
    assert result["scanned"] == 1
    assert len(store.list("t1", include_deleted=True)) == 1
    assert store.list("t2", include_deleted=True) == []
