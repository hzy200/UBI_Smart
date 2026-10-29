"""Workspace read-only ACL

Revision ID: 0013_workspace_acl
Revises: 0012_enterprise_identity
Create Date: 2026-10-28

Only additive table. Grants only make workspace metadata/revisions visible;
execution binding still requires the workspace owner.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_workspace_acl"
down_revision = "0012_enterprise_identity"
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


def _create_acl() -> None:
    op.create_table(
        "control_workspace_acl",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant", sa.String(128), nullable=False, server_default=""
        ),
        sa.Column(
            "workspace_id", sa.String(36), nullable=False, server_default=""
        ),
        sa.Column(
            "grantee", sa.String(128), nullable=False, server_default=""
        ),
        sa.Column(
            "permission",
            sa.String(16),
            nullable=False,
            server_default="read",
        ),
        sa.Column(
            "granted_by", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column(
            "created_at", sa.String(32), nullable=False, server_default=""
        ),
        sa.UniqueConstraint(
            "tenant",
            "workspace_id",
            "grantee",
            name="uq_workspace_acl_tenant_ws_grantee",
        ),
    )
    op.create_index(
        "ix_workspace_acl_tenant_grantee",
        "control_workspace_acl",
        ["tenant", "grantee"],
    )


def upgrade() -> None:
    if op.get_context().as_sql:
        _set_version("13")
        return
    inspector = sa.inspect(op.get_bind())
    if "control_workspace_acl" not in set(inspector.get_table_names()):
        _create_acl()
    _set_version("13")


def downgrade() -> None:
    # 授权是事实数据；回退只回写版本号，不删表。
    _set_version("12")
