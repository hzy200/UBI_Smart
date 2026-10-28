"""audit index lookup by tenant and record hash

Revision ID: 0009_audit_hash_index
Revises: 0008_session_unread_preview
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_audit_hash_index"
down_revision = "0008_session_unread_preview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_control_audit_tenant_hash",
        "control_audit_index",
        ["tenant", "record_hash"],
    )
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


def downgrade() -> None:
    op.drop_index(
        "ix_control_audit_tenant_hash",
        table_name="control_audit_index",
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
