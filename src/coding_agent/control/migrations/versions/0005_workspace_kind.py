"""workspace host/sandbox kind and A2L handle

Revision ID: 0005_workspace_kind
Revises: 0004_leases_idempotency
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_workspace_kind"
down_revision = "0004_leases_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "kind", sa.String(16), nullable=False, server_default="sandbox"
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "a2l_id", sa.String(64), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "root_handle", sa.String(128), nullable=False, server_default=""
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
        .values(value="5")
    )


def downgrade() -> None:
    op.drop_column("workspaces", "root_handle")
    op.drop_column("workspaces", "a2l_id")
    op.drop_column("workspaces", "kind")
    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="4")
    )
