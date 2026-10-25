"""revision pinned flag

Revision ID: 0003_revision_pinned
Revises: 0002_workspaces
Create Date: 2026-10-25

pinned 与 status 解耦：pin 一条 revision 不会改变它是否 active/superseded，
只影响自动清理。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_revision_pinned"
down_revision = "0002_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_revisions",
        sa.Column(
            "pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    meta = sa.table(
        "control_meta", sa.column("key"), sa.column("value")
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="3")
    )


def downgrade() -> None:
    op.drop_column("workspace_revisions", "pinned")
    meta = sa.table(
        "control_meta", sa.column("key"), sa.column("value")
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="2")
    )
