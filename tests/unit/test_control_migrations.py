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
    downgrade,
    has_column,
    has_table,
    upgrade,
)
from coding_agent.control.store import ControlStore  # noqa: E402

REVISION = "0015_password_reset"


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
    assert has_column(url, "control_users", "max_access_mode")
    assert has_column(url, "workspaces", "kind")
    assert has_column(url, "workspaces", "root_handle")
    assert has_table(url, "run_leases")
    assert has_table(url, "idempotency_keys")
    for table in (
        "control_user_identities",
        "control_oidc_states",
        "control_user_mfa",
        "control_mfa_challenges",
        "control_approvals",
        "control_workspace_acl",
        "control_password_resets",
    ):
        assert has_table(url, table)
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
    assert has_column(url, "control_users", "max_access_mode")
    assert has_column(url, "workspaces", "kind")
    assert has_column(url, "workspaces", "root_handle")
    assert has_table(url, "run_leases")
    assert has_table(url, "idempotency_keys")


def test_upgrade_from_0012_adds_workspace_acl(tmp_path) -> None:
    """跨版本升级：0012 只补 ACL 表，既有工作区不重建、不丢。"""
    url = _url(tmp_path)
    command.upgrade(alembic_config(url), "0012_enterprise_identity")
    assert has_table(url, "workspaces")
    assert not has_table(url, "control_workspace_acl")

    result = upgrade(url)
    assert result == {"action": "upgrade", "revision": REVISION}
    assert has_table(url, "workspaces")
    assert has_table(url, "control_workspace_acl")


def test_upgrade_from_0013_adds_user_access_limit(tmp_path) -> None:
    url = _url(tmp_path)
    command.upgrade(alembic_config(url), "0013_workspace_acl")
    assert not has_column(url, "control_users", "max_access_mode")

    result = upgrade(url)
    assert result == {"action": "upgrade", "revision": REVISION}
    assert has_column(url, "control_users", "max_access_mode")


def test_upgrade_from_0014_adds_password_resets(tmp_path) -> None:
    url = _url(tmp_path)
    command.upgrade(alembic_config(url), "0014_user_access_limit")
    assert not has_table(url, "control_password_resets")

    result = upgrade(url)
    assert result == {"action": "upgrade", "revision": REVISION}
    assert has_table(url, "control_password_resets")


def test_upgrade_from_0011_preserves_accounts_and_usage(tmp_path) -> None:
    """跨版本升级：0011 已有账号/用量升到 head 后必须原样保留。"""
    url = _url(tmp_path)
    command.upgrade(alembic_config(url), "0011_cny_reservations")
    engine = create_engine(url, future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO control_users "
                "(id, tenant, username, email, role, password_hash, "
                "status, created_at, updated_at, last_login_at) "
                "VALUES ('u1', 'default', 'alice', 'a@example.com', "
                "'member', 'x', 'active', '2026-10-28', '2026-10-28', '')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO control_user_usage_daily "
                "(tenant, user, day, input_tokens, output_tokens, "
                "worker_input_tokens, worker_output_tokens, cost_usd, "
                "billed_usd, runs, reserved_tokens, reserved_usd) "
                "VALUES ('default', 'alice', '2026-10-28', 10, 5, "
                "2, 1, 0.01, 0.01, 1, 0, 0)"
            )
        )
    engine.dispose()

    result = upgrade(url)
    assert result == {"action": "upgrade", "revision": REVISION}
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            user = connection.execute(
                text("SELECT username FROM control_users WHERE id = 'u1'")
            ).scalar_one()
            tokens = connection.execute(
                text(
                    "SELECT input_tokens + output_tokens "
                    "FROM control_user_usage_daily "
                    "WHERE tenant='default' AND user='alice' "
                    "AND day='2026-10-28'"
                )
            ).scalar_one()
        for table in (
            "control_user_identities",
            "control_oidc_states",
            "control_user_mfa",
            "control_mfa_challenges",
            "control_approvals",
            "control_workspace_acl",
            "control_password_resets",
        ):
            assert has_table(url, table)
    finally:
        engine.dispose()
    assert user == "alice"
    assert tokens == 15


def test_downgrade_returns_to_the_requested_revision(tmp_path) -> None:
    """降级是破坏性的：入口必须存在（否则只能靠备份整库恢复），且默认先备份。"""
    url = _url(tmp_path)
    assert upgrade(url)["revision"] == REVISION
    assert has_table(url, "workspaces")

    result = downgrade(url, BASELINE_REVISION, backup=False)
    assert result == {"action": "downgrade", "revision": BASELINE_REVISION}
    assert current_revision(url) == BASELINE_REVISION
    assert not has_table(url, "workspaces")
    assert not has_column(url, "control_sessions", "workspace_id")

    # 退回去还能再升回来。
    assert upgrade(url)["revision"] == REVISION
    assert has_table(url, "workspaces")


def test_downgrade_makes_a_backup_by_default(tmp_path) -> None:
    """默认备份：降级丢列丢表，没有备份它就是单向门。"""
    url = _url(tmp_path)
    upgrade(url)
    backup_dir = tmp_path / "backups"
    downgrade(url, BASELINE_REVISION, backup_dir=backup_dir)
    assert backup_dir.is_dir()
    assert list(backup_dir.iterdir())


def test_downgrade_refuses_unmanaged_database(tmp_path) -> None:
    """没有 alembic_version 就不知道自己在哪 —— 不能猜着往回退。"""
    url = _url(tmp_path)
    upgrade(url)
    engine = create_engine(url, future=True)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
    engine.dispose()

    with pytest.raises(ValueError, match="alebic|alembic_version|纳管"):
        downgrade(url, BASELINE_REVISION, backup=False)


def test_downgrade_requires_an_explicit_revision(tmp_path) -> None:
    url = _url(tmp_path)
    upgrade(url)
    with pytest.raises(ValueError, match="revision"):
        downgrade(url, "", backup=False)


def test_upgrade_rejects_empty_url() -> None:
    try:
        upgrade("")
    except ValueError as exc:
        assert "URL" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空 URL 应该报错")
