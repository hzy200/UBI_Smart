"""Read-only session share links

Revision ID: 0019_share_links
Revises: 0018_api_tokens_webhooks
Create Date: 2026-10-29

Additive only. Payload is the already-redacted snapshot; the URL token is
stored as sha256 only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_share_links"
down_revision = "0018_api_tokens_webhooks"
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
        _set_version("19")
        return
    inspector = sa.inspect(op.get_bind())
    if "control_share_links" not in set(inspector.get_table_names()):
        op.create_table(
            "control_share_links",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "token_hash",
                sa.String(64),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "tenant", sa.String(128), nullable=False, server_default=""
            ),
            sa.Column(
                "thread_id", sa.String(64), nullable=False, server_default=""
            ),
            sa.Column(
                "created_by",
                sa.String(64),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "title", sa.String(160), nullable=False, server_default=""
            ),
            sa.Column("payload", sa.Text(), nullable=False),
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
                "revoked_at",
                sa.String(32),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "views",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.UniqueConstraint(
                "token_hash", name="uq_share_token_hash"
            ),
        )
        op.create_index(
            "ix_control_share_links_tenant",
            "control_share_links",
            ["tenant"],
        )
        op.create_index(
            "ix_control_share_links_tenant_thread",
            "control_share_links",
            ["tenant", "thread_id"],
        )
        op.create_index(
            "ix_control_share_links_expires",
            "control_share_links",
            ["expires_at"],
        )
    _set_version("19")


def downgrade() -> None:
    op.drop_table("control_share_links")
    _set_version("18")
