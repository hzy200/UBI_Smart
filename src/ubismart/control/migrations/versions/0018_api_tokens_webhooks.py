"""Personal access tokens and outbound webhooks

Revision ID: 0018_api_tokens_webhooks
Revises: 0017_push_subscriptions
Create Date: 2026-10-29

Additive only. PATs store sha256 only; webhook secrets must remain
recoverable because they sign outbound payloads.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_api_tokens_webhooks"
down_revision = "0017_push_subscriptions"
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
        _set_version("18")
        return
    inspector = sa.inspect(op.get_bind())
    names = set(inspector.get_table_names())

    if "control_api_tokens" not in names:
        op.create_table(
            "control_api_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "tenant", sa.String(128), nullable=False, server_default=""
            ),
            sa.Column(
                "user_id", sa.String(36), nullable=False, server_default=""
            ),
            sa.Column(
                "name", sa.String(64), nullable=False, server_default=""
            ),
            sa.Column(
                "token_hash",
                sa.String(64),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "prefix", sa.String(32), nullable=False, server_default=""
            ),
            sa.Column(
                "scopes",
                sa.String(128),
                nullable=False,
                server_default="read",
            ),
            sa.Column(
                "created_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "last_used_at",
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
                "revoked_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
            sa.UniqueConstraint(
                "token_hash", name="uq_api_token_hash"
            ),
        )
        op.create_index(
            "ix_control_api_tokens_tenant",
            "control_api_tokens",
            ["tenant"],
        )
        op.create_index(
            "ix_control_api_tokens_user_id",
            "control_api_tokens",
            ["user_id"],
        )
        op.create_index(
            "ix_control_api_tokens_tenant_active",
            "control_api_tokens",
            ["tenant", "revoked_at"],
        )

    if "control_webhooks" not in names:
        op.create_table(
            "control_webhooks",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "tenant", sa.String(128), nullable=False, server_default=""
            ),
            sa.Column(
                "name", sa.String(64), nullable=False, server_default=""
            ),
            sa.Column(
                "url", sa.String(1024), nullable=False, server_default=""
            ),
            sa.Column(
                "secret", sa.String(128), nullable=False, server_default=""
            ),
            sa.Column(
                "events",
                sa.String(128),
                nullable=False,
                server_default="run_finished",
            ),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
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
            sa.Column(
                "last_sent_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "last_status",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "last_error",
                sa.String(255),
                nullable=False,
                server_default="",
            ),
        )
        op.create_index(
            "ix_control_webhooks_tenant",
            "control_webhooks",
            ["tenant"],
        )
        op.create_index(
            "ix_control_webhooks_tenant_enabled",
            "control_webhooks",
            ["tenant", "enabled"],
        )
    _set_version("18")


def downgrade() -> None:
    op.drop_table("control_webhooks")
    op.drop_table("control_api_tokens")
    _set_version("17")
