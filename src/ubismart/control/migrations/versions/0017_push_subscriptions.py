"""Browser Web Push subscriptions

Revision ID: 0017_push_subscriptions
Revises: 0016_notifications_search
Create Date: 2026-10-29

Additive only. The endpoint can exceed 255 chars, so uniqueness uses a
sha256 hash instead of the endpoint text itself.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_push_subscriptions"
down_revision = "0016_notifications_search"
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
        _set_version("17")
        return
    inspector = sa.inspect(op.get_bind())
    if "control_push_subscriptions" not in set(
        inspector.get_table_names()
    ):
        op.create_table(
            "control_push_subscriptions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "tenant", sa.String(128), nullable=False, server_default=""
            ),
            sa.Column(
                "endpoint_hash",
                sa.String(64),
                nullable=False,
                server_default="",
            ),
            sa.Column("endpoint", sa.Text(), nullable=False),
            sa.Column(
                "p256dh",
                sa.String(255),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "auth", sa.String(128), nullable=False, server_default=""
            ),
            sa.Column(
                "user_agent",
                sa.String(255),
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
                "updated_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
            sa.UniqueConstraint(
                "tenant",
                "endpoint_hash",
                name="uq_push_tenant_endpoint",
            ),
        )
        op.create_index(
            "ix_control_push_subscriptions_tenant",
            "control_push_subscriptions",
            ["tenant"],
        )
    _set_version("17")


def downgrade() -> None:
    op.drop_table("control_push_subscriptions")
    _set_version("16")
