"""session unread and last message preview

Revision ID: 0008_session_unread_preview
Revises: 0007_session_meta
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_session_unread_preview"
down_revision = "0007_session_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "control_sessions",
        sa.Column("unread", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "control_sessions",
        sa.Column(
            "last_message", sa.String(160), nullable=False, server_default=""
        ),
    )
    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="8")
    )


def downgrade() -> None:
    op.drop_column("control_sessions", "last_message")
    op.drop_column("control_sessions", "unread")
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
