"""persist session workspace binding

Revision ID: 0006_session_workspace_binding
Revises: 0005_workspace_kind
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_session_workspace_binding"
down_revision = "0005_workspace_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 会话与工作区的绑定是不可变的；host 会话必须把 kind/a2l/handle
    # 一并落库，否则工作区目录丢失或本地 catalog 重建后会退回 sandbox。
    op.add_column(
        "control_sessions",
        sa.Column(
            "workspace_kind", sa.String(16), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "control_sessions",
        sa.Column(
            "a2l_id", sa.String(64), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "control_sessions",
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
        .values(value="6")
    )


def downgrade() -> None:
    op.drop_column("control_sessions", "root_handle")
    op.drop_column("control_sessions", "a2l_id")
    op.drop_column("control_sessions", "workspace_kind")
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
