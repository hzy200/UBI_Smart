"""Notifications and cross-session search index

Revision ID: 0016_notifications_search
Revises: 0015_password_reset
Create Date: 2026-10-29

Additive only. SQLite builds an FTS5 trigram table for substring search;
MySQL uses the ordinary indexed row plus LIKE (there is no FTS5 there).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_notifications_search"
down_revision = "0015_password_reset"
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


def upgrade() -> None:
    if op.get_context().as_sql:
        _set_version("16")
        return
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = set(inspector.get_table_names())

    if "control_notifications" not in names:
        op.create_table(
            "control_notifications",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "tenant", sa.String(128), nullable=False, server_default=""
            ),
            sa.Column("dedupe_key", sa.String(160), nullable=True),
            sa.Column(
                "kind", sa.String(24), nullable=False, server_default="info"
            ),
            sa.Column(
                "title", sa.String(160), nullable=False, server_default=""
            ),
            sa.Column(
                "body", sa.String(500), nullable=False, server_default=""
            ),
            sa.Column(
                "thread_id", sa.String(64), nullable=False, server_default=""
            ),
            sa.Column(
                "path", sa.String(512), nullable=False, server_default=""
            ),
            sa.Column(
                "read",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "created_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
            sa.UniqueConstraint(
                "tenant",
                "dedupe_key",
                name="uq_notification_tenant_dedupe",
            ),
        )
        op.create_index(
            "ix_control_notifications_tenant_created",
            "control_notifications",
            ["tenant", "created_at"],
        )

    if "control_session_search" not in names:
        op.create_table(
            "control_session_search",
            sa.Column("tenant", sa.String(128), primary_key=True),
            sa.Column("thread_id", sa.String(64), primary_key=True),
            sa.Column(
                "title", sa.String(160), nullable=False, server_default=""
            ),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column(
                "updated_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
        )
        op.create_index(
            "ix_control_session_search_tenant_updated",
            "control_session_search",
            ["tenant", "updated_at"],
        )

    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS control_session_fts "
            "USING fts5(tenant UNINDEXED, thread_id UNINDEXED, title, body, "
            "tokenize='trigram')"
        )
    _set_version("16")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS control_session_fts")
    op.drop_table("control_session_search")
    op.drop_table("control_notifications")
    _set_version("15")
