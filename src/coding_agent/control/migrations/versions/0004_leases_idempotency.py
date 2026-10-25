"""run leases and idempotency keys

Revision ID: 0004_leases_idempotency
Revises: 0003_revision_pinned
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_leases_idempotency"
down_revision = "0003_revision_pinned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_leases",
        sa.Column("session_key", sa.String(192), primary_key=True),
        sa.Column(
            "lease_id", sa.String(36), nullable=False, server_default=""
        ),
        sa.Column(
            "owner", sa.String(128), nullable=False, server_default=""
        ),
        sa.Column(
            "expires_at", sa.String(32), nullable=False, server_default=""
        ),
        sa.Column(
            "created_at", sa.String(32), nullable=False, server_default=""
        ),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column(
            "scope", sa.String(32), nullable=False, server_default=""
        ),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="running"
        ),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column(
            "expires_at", sa.String(32), nullable=False, server_default=""
        ),
        sa.Column(
            "created_at", sa.String(32), nullable=False, server_default=""
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
        .values(value="4")
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("run_leases")
    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="3")
    )
