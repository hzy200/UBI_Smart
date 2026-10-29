"""Self-service password reset tokens

Revision ID: 0015_password_reset
Revises: 0014_user_access_limit
Create Date: 2026-10-29

Only additive table. `downgrade` rewinds the marker; unused reset tokens are
short-lived data, but we do not delete the table automatically.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_password_reset"
down_revision = "0014_user_access_limit"
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
        _set_version("15")
        return
    inspector = sa.inspect(op.get_bind())
    if "control_password_resets" not in set(inspector.get_table_names()):
        op.create_table(
            "control_password_resets",
            sa.Column("token_hash", sa.String(64), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "created_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "expires_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "used_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
        )
        op.create_index(
            "ix_control_password_resets_user",
            "control_password_resets",
            ["user_id"],
        )
        op.create_index(
            "ix_control_password_resets_expires",
            "control_password_resets",
            ["expires_at"],
        )
    _set_version("15")


def downgrade() -> None:
    _set_version("14")
