"""控制面迁移的 MySQL 方言离线编译。

不需要真实 MySQL：Alembic offline 模式只生成 SQL，用来提前发现
TEXT 默认值、索引名、列类型等方言问题。
"""

from __future__ import annotations

import contextlib
import io

import pytest

pytest.importorskip("alembic")

from alembic import command  # noqa: E402

from coding_agent.control.migrate import alembic_config  # noqa: E402


def _mysql_sql() -> str:
    config = alembic_config(
        "mysql+pymysql://user:pass@127.0.0.1:3306/coding_agent_test"
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def test_mysql_ddl_contains_all_tables() -> None:
    sql = _mysql_sql().upper()
    for table in (
        "CONTROL_SESSIONS",
        "WORKSPACES",
        "WORKSPACE_REVISIONS",
        "RUN_LEASES",
        "IDEMPOTENCY_KEYS",
        "CONTROL_USERS",
        "CONTROL_AUTH_SESSIONS",
        "CONTROL_AUTH_EVENTS",
        "CONTROL_PROVIDER_KEYS",
        "CONTROL_USER_QUOTAS",
        "CONTROL_USER_USAGE_DAILY",
        "CONTROL_QUOTA_RESERVATIONS",
        "CONTROL_USAGE_EVENTS",
        "CONTROL_USAGE_CURSOR",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "PINNED" in sql
    assert "WORKSPACE_ID" in sql
    # MySQL 不接受 TEXT/BLOB 的普通默认值。
    assert "TEXT NOT NULL DEFAULT" not in sql
    assert "UTF8MB4" not in sql  # 建库字符集由部署脚本负责


def test_mysql_ddl_has_incremental_columns() -> None:
    sql = _mysql_sql().upper()
    assert "ALTER TABLE CONTROL_SESSIONS ADD COLUMN WORKSPACE_ID" in sql
