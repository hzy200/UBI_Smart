"""CNY quotas, multi-instance reservations and auth events

Revision ID: 0011_cny_reservations_auth_events
Revises: 0010_accounts_usage_quota
Create Date: 2026-10-28

This migration is intentionally idempotent for online databases: 0010 in this
repository already contains the final schema, while databases created from an
older 0010 need the incremental changes. Offline (`--sql`) mode only updates
the schema version because inspection is unavailable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_cny_reservations_auth_events"
down_revision = "0010_accounts_usage_quota"
branch_labels = None
depends_on = None


def _set_version(value: str) -> None:
    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value=value)
    )


def _create_auth_events() -> None:
    op.create_table(
        "control_auth_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False, server_default=""),
        sa.Column("username", sa.String(64), nullable=False, server_default=""),
        sa.Column("event", sa.String(32), nullable=False, server_default=""),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("request_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("ts", sa.String(32), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_control_auth_events_ts", "control_auth_events", ["tenant", "ts"]
    )
    op.create_index(
        "ix_control_auth_events_user_ts",
        "control_auth_events",
        ["tenant", "username", "ts"],
    )


def _create_reservations() -> None:
    op.create_table(
        "control_quota_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False, server_default=""),
        sa.Column("user", sa.String(64), nullable=False, server_default=""),
        sa.Column("day", sa.String(10), nullable=False, server_default=""),
        sa.Column("tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("owner", sa.String(64), nullable=False, server_default=""),
        sa.Column("expires_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_control_quota_reservations_user_day",
        "control_quota_reservations",
        ["tenant", "user", "day"],
    )
    op.create_index(
        "ix_control_quota_reservations_expires",
        "control_quota_reservations",
        ["expires_at"],
    )


def _rebuild_cursor() -> None:
    """旧 0010 的游标主键没有 user，重建表并保留累计值。"""
    op.rename_table("control_usage_cursor", "control_usage_cursor_legacy")
    op.create_table(
        "control_usage_cursor",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("user", sa.String(64), primary_key=True),
        sa.Column("thread_id", sa.String(128), primary_key=True),
        sa.Column("run_epoch", sa.String(36), nullable=False, server_default=""),
        sa.Column("tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "worker_input_tokens", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "worker_output_tokens", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("billed_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.execute(
        "INSERT INTO control_usage_cursor "
        "(tenant, user, thread_id, run_epoch, tokens, input_tokens, "
        "output_tokens, worker_input_tokens, worker_output_tokens, "
        "cost_usd, billed_usd) "
        "SELECT tenant, '', thread_id, run_epoch, tokens, input_tokens, "
        "output_tokens, worker_input_tokens, worker_output_tokens, "
        "cost_usd, billed_usd FROM control_usage_cursor_legacy"
    )
    op.drop_table("control_usage_cursor_legacy")


def upgrade() -> None:
    if op.get_context().as_sql:
        _set_version("11")
        return

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "control_auth_events" not in tables:
        _create_auth_events()
    if "control_quota_reservations" not in tables:
        _create_reservations()

    if "control_usage_events" in tables:
        columns = {
            item["name"]
            for item in inspector.get_columns("control_usage_events")
        }
        if "user" not in columns:
            op.add_column(
                "control_usage_events",
                sa.Column(
                    "user",
                    sa.String(64),
                    nullable=False,
                    server_default="",
                ),
            )

    if "control_usage_cursor" in tables:
        pk = inspector.get_pk_constraint("control_usage_cursor")
        pk_columns = list(pk.get("constrained_columns") or [])
        if "user" not in pk_columns:
            _rebuild_cursor()

    if "control_user_quotas" in tables:
        columns = {
            item["name"]
            for item in inspector.get_columns("control_user_quotas")
        }
        if "daily_cost_cny" not in columns:
            op.add_column(
                "control_user_quotas",
                sa.Column(
                    "daily_cost_cny",
                    sa.Numeric(18, 4),
                    nullable=False,
                    server_default="0",
                ),
            )
            if "daily_cost_usd" in columns:
                # 旧库按 7.2 一次性迁移；之后以 AGENT_USD_TO_CNY 为准。
                op.execute(
                    "UPDATE control_user_quotas "
                    "SET daily_cost_cny = daily_cost_usd * 7.2"
                )
    _set_version("11")


def downgrade() -> None:
    # 对象是 0010 最终 schema 的一部分；回退只标记版本，避免误删数据。
    _set_version("10")
