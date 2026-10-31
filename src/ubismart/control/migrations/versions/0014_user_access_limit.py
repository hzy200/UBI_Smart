"""Per-user maximum session access mode

Revision ID: 0014_user_access_limit
Revises: 0013_workspace_acl
Create Date: 2026-10-28

Additive column on control_users. `downgrade` only rewinds the version marker
because access limits are policy data, not something to silently drop.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_user_access_limit"
down_revision = "0013_workspace_acl"
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
        _set_version("14")
        return
    inspector = sa.inspect(op.get_bind())
    if "control_users" in set(inspector.get_table_names()):
        columns = {
            item["name"]
            for item in inspector.get_columns("control_users")
        }
        if "max_access_mode" not in columns:
            op.add_column(
                "control_users",
                sa.Column(
                    "max_access_mode",
                    sa.String(16),
                    nullable=False,
                    server_default="write_auto",
                ),
            )
    _set_version("14")


def downgrade() -> None:
    # 访问上限是策略数据；回退只回写版本号，不删列。
    _set_version("13")
