"""control schema baseline

Revision ID: 0001_control_schema
Revises:
Create Date: 2026-10-24

与 `ubismart.control.schema.Base` 对齐。旧库（Phase B 用 create_all
建的）由 migrate.upgrade 先 stamp 到这个版本，不重复建表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_control_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_meta",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(255), nullable=False, server_default=""),
    )

    op.create_table(
        "control_sessions",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("thread_id", sa.String(64), primary_key=True),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column(
            "access_mode",
            sa.String(16),
            nullable=False,
            server_default="read_only",
        ),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "provider", sa.String(128), nullable=False, server_default=""
        ),
        sa.Column(
            "uploaded_bytes",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("title", sa.String(120), nullable=False, server_default=""),
        sa.Column(
            "deleted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at", sa.String(32), nullable=False, server_default=""
        ),
        sa.Column(
            "updated_at", sa.String(32), nullable=False, server_default=""
        ),
    )
    op.create_index(
        "ix_control_sessions_tenant_deleted",
        "control_sessions",
        ["tenant", "deleted"],
    )

    op.create_table(
        "control_runs",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column(
            "thread_id", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="running"
        ),
        sa.Column(
            "started_at", sa.String(32), nullable=False, server_default=""
        ),
        sa.Column(
            "finished_at", sa.String(32), nullable=False, server_default=""
        ),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_control_runs_tenant_thread",
        "control_runs",
        ["tenant", "thread_id"],
    )

    op.create_table(
        "control_audit_index",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "tenant", sa.String(128), nullable=False, server_default=""
        ),
        sa.Column(
            "thread_id", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("ts", sa.String(32), nullable=False, server_default=""),
        sa.Column("kind", sa.String(32), nullable=False, server_default=""),
        sa.Column("tool", sa.String(64), nullable=False, server_default=""),
        sa.Column("level", sa.String(16), nullable=False, server_default=""),
        sa.Column(
            "decision", sa.String(16), nullable=False, server_default=""
        ),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column(
            "record_hash", sa.String(64), nullable=False, server_default=""
        ),
    )
    op.create_index(
        "ix_control_audit_tenant_thread",
        "control_audit_index",
        ["tenant", "thread_id", "ts"],
    )

    op.create_table(
        "control_provider_profiles",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "priority", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "input_cost_per_1m",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "output_cost_per_1m",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("max_tokens", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_cost", sa.Float(), nullable=False, server_default="0"),
        # MySQL 不允许 TEXT 列带字面量默认值；默认值由 ORM 侧提供。
        sa.Column("capabilities", sa.Text(), nullable=False),
        sa.Column("task_types", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.String(32), nullable=False, server_default=""
        ),
        sa.UniqueConstraint(
            "tenant", "name", name="uq_provider_tenant_name"
        ),
    )

    op.create_table(
        "control_usage_daily",
        sa.Column("tenant", sa.String(128), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("provider", sa.String(128), primary_key=True),
        sa.Column("model", sa.String(128), primary_key=True),
        sa.Column(
            "input_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "output_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cost_usd", sa.Float(), nullable=False, server_default="0"
        ),
    )

    # MySQL 里 key 是保留字；用 table 构造让方言负责引用标识符。
    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.bulk_insert(meta, [{"key": "schema_version", "value": "1"}])


def downgrade() -> None:
    op.drop_table("control_usage_daily")
    op.drop_table("control_provider_profiles")
    op.drop_index(
        "ix_control_audit_tenant_thread", table_name="control_audit_index"
    )
    op.drop_table("control_audit_index")
    op.drop_index(
        "ix_control_runs_tenant_thread", table_name="control_runs"
    )
    op.drop_table("control_runs")
    op.drop_index(
        "ix_control_sessions_tenant_deleted", table_name="control_sessions"
    )
    op.drop_table("control_sessions")
    op.drop_table("control_meta")
