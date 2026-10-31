"""workspace registry

Revision ID: 0002_workspaces
Revises: 0001_control_schema
Create Date: 2026-10-25

RFC-0001：工作区成为一等实体。

- 新增 `workspaces` 与 `workspace_revisions`；
- `control_sessions` 增加可空回填的 `workspace_id`；
- 不删除旧 `workspace` 列，关闭总开关时行为不变。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_workspaces"
down_revision = "0001_control_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("workspace_id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant", sa.String(128), nullable=False, server_default=""
        ),
        sa.Column("owner", sa.String(128), nullable=False, server_default=""),
        sa.Column("slug", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "display_name",
            sa.String(120),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "source", sa.String(24), nullable=False, server_default="upload"
        ),
        # MySQL 不允许 TEXT 带字面量默认值；默认值由 ORM 侧提供。
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column(
            "root_kind", sa.String(16), nullable=False, server_default="ext4"
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="provisioning",
        ),
        sa.Column(
            "access_mode",
            sa.String(16),
            nullable=False,
            server_default="read_only",
        ),
        sa.Column(
            "quota_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "used_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "file_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "degraded_fs",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "legacy", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at", sa.String(32), nullable=False, server_default=""
        ),
        sa.Column(
            "updated_at", sa.String(32), nullable=False, server_default=""
        ),
        sa.UniqueConstraint(
            "tenant", "owner", "slug", name="uq_workspace_tenant_owner_slug"
        ),
    )
    op.create_index(
        "ix_workspaces_tenant_status",
        "workspaces",
        ["tenant", "status"],
    )
    op.create_index(
        "ix_workspaces_owner", "workspaces", ["tenant", "owner"]
    )

    op.create_table(
        "workspace_revisions",
        sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id", sa.String(36), nullable=False, server_default=""
        ),
        sa.Column(
            "source", sa.String(24), nullable=False, server_default="upload"
        ),
        sa.Column("actor", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "git_commit", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("archive_path", sa.Text(), nullable=False),
        sa.Column(
            "archive_sha256",
            sa.String(64),
            nullable=False,
            server_default="",
        ),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "file_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "scan_status",
            sa.String(16),
            nullable=False,
            server_default="skipped",
        ),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="active"
        ),
        sa.Column(
            "created_at", sa.String(32), nullable=False, server_default=""
        ),
    )
    op.create_index(
        "ix_workspace_revisions_workspace_created",
        "workspace_revisions",
        ["workspace_id", "created_at"],
    )

    op.add_column(
        "control_sessions",
        sa.Column(
            "workspace_id",
            sa.String(36),
            nullable=False,
            server_default="",
        ),
    )
    op.create_index(
        "ix_control_sessions_workspace_id",
        "control_sessions",
        ["workspace_id"],
    )

    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="2")
    )


def downgrade() -> None:
    op.drop_index(
        "ix_control_sessions_workspace_id",
        table_name="control_sessions",
    )
    op.drop_column("control_sessions", "workspace_id")

    op.drop_index(
        "ix_workspace_revisions_workspace_created",
        table_name="workspace_revisions",
    )
    op.drop_table("workspace_revisions")

    op.drop_index("ix_workspaces_owner", table_name="workspaces")
    op.drop_index("ix_workspaces_tenant_status", table_name="workspaces")
    op.drop_table("workspaces")

    meta = sa.table(
        "control_meta",
        sa.column("key", sa.String(64)),
        sa.column("value", sa.String(255)),
    )
    op.execute(
        meta.update()
        .where(meta.c.key == "schema_version")
        .values(value="1")
    )
