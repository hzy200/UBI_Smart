"""session pin/archive/tags

Revision ID: 0007_session_meta
Revises: 0006_session_workspace_binding
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_session_meta"
down_revision = "0006_session_workspace_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "control_sessions",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "control_sessions",
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # MySQL 不允许 TEXT 列带字面量默认值；tags 最多 8 个短标签，用 VARCHAR。
    op.add_column(
        "control_sessions",
        sa.Column("tags", sa.String(1024), nullable=False, server_default=""),
    )
    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="7")
    )


def downgrade() -> None:
    op.drop_column("control_sessions", "tags")
    op.drop_column("control_sessions", "archived")
    op.drop_column("control_sessions", "pinned")
    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="6")
    )
