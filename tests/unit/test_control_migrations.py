"""Phase D3：Alembic 迁移、旧库 stamp 与增量升级。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("alembic")

from alembic import command  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

from coding_agent.control.migrate import (  # noqa: E402
    BASELINE_REVISION,
    alembic_config,
    current_revision,
    has_column,
    has_table,
    upgrade,
)
from coding_agent.control.store import ControlStore  # noqa: E402

REVISION = "0011_cny_reservations_auth_events"


def _url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'control.db'}"


def test_upgrade_creates_schema_and_is_idempotent(tmp_path) -> None:
    url = _url(tmp_path)
    result = upgrade(url)
    assert result == {"action": "upgrade", "revision": REVISION}
    assert has_table(url, "control_meta")
    assert has_table(url, "control_sessions")
    assert has_table(url, "workspaces")
    assert has_table(url, "workspace_revisions")
    assert has_column(url, "control_sessions", "workspace_id")
    assert has_column(url, "control_sessions", "unread")
    assert has_column(url, "control_sessions", "last_message")
    assert has_column(url, "control_sessions", "workspace_kind")
    assert has_column(url, "control_sessions", "a2l_id")
    assert has_column(url, "control_sessions", "root_handle")
    assert has_column(url, "control_sessions", "pinned")
    assert has_column(url, "control_sessions", "archived")
    assert has_column(url, "control_sessions", "tags")
    assert has_column(url, "workspace_revisions", "pinned")
    assert has_column(url, "workspaces", "kind")
    assert has_column(url, "workspaces", "root_handle")
    assert has_table(url, "run_leases")
    assert has_table(url, "idempotency_keys")
    assert current_revision(url) == REVISION

    engine = create_engine(url, future=True)
    try:
        indexes = {
            item["name"]
            for item in inspect(engine).get_indexes("control_audit_index")
        }
    finally:
        engine.dispose()
    assert "ix_control_audit_tenant_hash" in indexes

    again = upgrade(url)
    assert again == {"action": "upgrade", "revision": REVISION}

    store = ControlStore(url, auto_create=False)
    store.save_sessions(
        "local",
        [{
            "thread_id": "t1",
            "title": "迁移后",
            "pinned": True,
            "archived": True,
            "tags": ["重要", "前端"],
            "unread": True,
            "last_message": "最后一条消息",
        }],
        set(),
    )
    rows, deleted = store.load_sessions("local")
    assert rows[0]["title"] == "迁移后"
    assert rows[0]["pinned"] is True
    assert rows[0]["archived"] is True
    assert rows[0]["tags"] == ["重要", "前端"]
    assert rows[0]["unread"] is True
    assert rows[0]["last_message"] == "最后一条消息"
    assert deleted == set()
    store.close()


def test_current_create_all_is_only_stamped(tmp_path) -> None:
    """新 create_all 已建出最新结构；只补 alembic_version，不重复建表。"""
    url = _url(tmp_path)
    store = ControlStore(url)  # 新模型路径
    store.save_sessions(
        "local", [{"thread_id": "new", "title": "新库"}], set()
    )
    store.close()
    assert current_revision(url) is None

    result = upgrade(url)
    assert result == {"action": "stamp", "revision": REVISION}
    assert current_revision(url) == REVISION


def test_phase_b_baseline_is_upgraded_incrementally(tmp_path) -> None:
    """模拟 Phase B 旧库：只有 0001 的表，没有 alembic_version。"""
    url = _url(tmp_path)
    command.upgrade(alembic_config(url), BASELINE_REVISION)
    engine = create_engine(url, future=True)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
    engine.dispose()
    assert current_revision(url) is None
    assert not has_table(url, "workspaces")
    assert not has_column(url, "control_sessions", "workspace_id")

    result = upgrade(url)
    assert result == {"action": "upgrade", "revision": REVISION}
    assert current_revision(url) == REVISION
    assert has_table(url, "workspaces")
    assert has_column(url, "control_sessions", "workspace_id")
    assert has_column(url, "control_sessions", "workspace_kind")
    assert has_column(url, "control_sessions", "a2l_id")
    assert has_column(url, "control_sessions", "root_handle")
    assert has_column(url, "control_sessions", "pinned")
    assert has_column(url, "control_sessions", "archived")
    assert has_column(url, "control_sessions", "tags")
    assert has_column(url, "workspace_revisions", "pinned")
    assert has_column(url, "workspaces", "kind")
    assert has_column(url, "workspaces", "root_handle")
    assert has_table(url, "run_leases")
    assert has_table(url, "idempotency_keys")


def test_upgrade_rejects_empty_url() -> None:
    try:
        upgrade("")
    except ValueError as exc:
        assert "URL" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空 URL 应该报错")
