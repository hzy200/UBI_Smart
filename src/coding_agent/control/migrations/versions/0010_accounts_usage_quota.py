"""enterprise accounts, encrypted provider keys, usage and quotas

Revision ID: 0010_accounts_usage_quota
Revises: 0009_audit_hash_index
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_accounts_usage_quota"
down_revision = "0009_audit_hash_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False, server_default="default"),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, server_default=""),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("password_hash", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("last_login_at", sa.String(32), nullable=False, server_default=""),
        sa.UniqueConstraint("tenant", "username", name="uq_control_user_tenant_name"),
    )
    op.create_index("ix_control_users_tenant", "control_users", ["tenant"])
    op.create_index(
        "ix_control_users_status", "control_users", ["tenant", "status"]
    )

    op.create_table(
        "control_auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("csrf_secret", sa.String(64), nullable=False, server_default=""),
        sa.Column("expires_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("revoked_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_control_auth_sessions_user", "control_auth_sessions", ["user_id"]
    )
    op.create_index(
        "ix_control_auth_sessions_expires",
        "control_auth_sessions",
        ["expires_at"],
    )

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

    op.create_table(
        "control_provider_keys",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("user", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False, server_default=""),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last4", sa.String(8), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(32), nullable=False, server_default=""),
    )

    op.create_table(
        "control_user_quotas",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("user", sa.String(64), primary_key=True),
        sa.Column("daily_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("daily_cost_cny", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(32), nullable=False, server_default=""),
    )

    op.create_table(
        "control_user_usage_daily",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("user", sa.String(64), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
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
        sa.Column("runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_control_user_usage_day",
        "control_user_usage_daily",
        ["tenant", "day"],
    )

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

    op.create_table(
        "control_usage_events",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("record_hash", sa.String(64), primary_key=True),
        sa.Column("user", sa.String(64), nullable=False, server_default=""),
        sa.Column("thread_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("run_epoch", sa.String(36), nullable=False, server_default=""),
        sa.Column("ts", sa.String(32), nullable=False, server_default=""),
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
    op.create_index(
        "ix_control_usage_events_ts",
        "control_usage_events",
        ["tenant", "ts"],
    )

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

    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="10")
    )


def downgrade() -> None:
    op.drop_table("control_usage_cursor")
    op.drop_index(
        "ix_control_quota_reservations_expires",
        table_name="control_quota_reservations",
    )
    op.drop_index(
        "ix_control_quota_reservations_user_day",
        table_name="control_quota_reservations",
    )
    op.drop_table("control_quota_reservations")
    op.drop_index(
        "ix_control_usage_events_ts", table_name="control_usage_events"
    )
    op.drop_table("control_usage_events")
    op.drop_index("ix_control_user_usage_day", table_name="control_user_usage_daily")
    op.drop_table("control_user_usage_daily")
    op.drop_table("control_user_quotas")
    op.drop_table("control_provider_keys")
    op.drop_index(
        "ix_control_auth_events_user_ts",
        table_name="control_auth_events",
    )
    op.drop_index(
        "ix_control_auth_events_ts", table_name="control_auth_events"
    )
    op.drop_table("control_auth_events")
    op.drop_index(
        "ix_control_auth_sessions_expires",
        table_name="control_auth_sessions",
    )
    op.drop_index("ix_control_auth_sessions_user", table_name="control_auth_sessions")
    op.drop_table("control_auth_sessions")
    op.drop_index("ix_control_users_status", table_name="control_users")
    op.drop_index("ix_control_users_tenant", table_name="control_users")
    op.drop_table("control_users")

    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="9")
    )
