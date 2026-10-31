"""Enterprise identity: OIDC state, identities, TOTP MFA, four-eyes approvals

Revision ID: 0012_enterprise_identity
Revises: 0011_cny_reservations
Create Date: 2026-10-28

Only additive tables. The migration is idempotent for databases that already
got the final schema from `create_all`; `downgrade` only rewinds the version
marker so identity bindings/approval history are not destroyed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_enterprise_identity"
down_revision = "0011_cny_reservations"
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


def _create_identities() -> None:
    op.create_table(
        "control_user_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False, server_default="default"),
        sa.Column("user_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("issuer", sa.String(255), nullable=False, server_default=""),
        sa.Column("subject", sa.String(255), nullable=False, server_default=""),
        sa.Column("email", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
        sa.UniqueConstraint(
            "tenant",
            "issuer",
            "subject",
            name="uq_user_identity_tenant_issuer_subject",
        ),
    )
    op.create_index(
        "ix_control_user_identities_tenant",
        "control_user_identities",
        ["tenant"],
    )
    op.create_index(
        "ix_control_user_identities_user",
        "control_user_identities",
        ["user_id"],
    )


def _create_oidc_states() -> None:
    op.create_table(
        "control_oidc_states",
        sa.Column("state", sa.String(64), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False, server_default="default"),
        sa.Column("nonce", sa.String(128), nullable=False, server_default=""),
        sa.Column("code_verifier", sa.String(128), nullable=False, server_default=""),
        sa.Column("redirect_uri", sa.String(512), nullable=False, server_default=""),
        sa.Column("return_to", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("expires_at", sa.String(32), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_control_oidc_states_expires",
        "control_oidc_states",
        ["expires_at"],
    )


def _create_user_mfa() -> None:
    op.create_table(
        "control_user_mfa",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False, server_default=""),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recovery_hashes", sa.Text(), nullable=False),
        sa.Column("last_used_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(32), nullable=False, server_default=""),
    )


def _create_mfa_challenges() -> None:
    op.create_table(
        "control_mfa_challenges",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("expires_at", sa.String(32), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_control_mfa_challenges_user",
        "control_mfa_challenges",
        ["user_id"],
    )
    op.create_index(
        "ix_control_mfa_challenges_expires",
        "control_mfa_challenges",
        ["expires_at"],
    )


def _create_approvals() -> None:
    op.create_table(
        "control_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.String(128), nullable=False, server_default="default"),
        sa.Column("thread_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("call_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("tool", sa.String(64), nullable=False, server_default=""),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("level", sa.String(32), nullable=False, server_default=""),
        sa.Column("level_int", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requested_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("decided_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(32), nullable=False, server_default=""),
        sa.Column("expires_at", sa.String(32), nullable=False, server_default=""),
        sa.UniqueConstraint(
            "tenant",
            "thread_id",
            "call_id",
            name="uq_approval_tenant_thread_call",
        ),
    )
    op.create_index(
        "ix_control_approvals_tenant",
        "control_approvals",
        ["tenant"],
    )
    op.create_index(
        "ix_control_approvals_pending",
        "control_approvals",
        ["tenant", "status", "created_at"],
    )


def upgrade() -> None:
    if op.get_context().as_sql:
        _set_version("12")
        return

    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "control_user_identities" not in tables:
        _create_identities()
    if "control_oidc_states" not in tables:
        _create_oidc_states()
    else:
        columns = {
            item["name"]
            for item in inspector.get_columns("control_oidc_states")
        }
        if "return_to" not in columns:
            op.add_column(
                "control_oidc_states",
                sa.Column(
                    "return_to",
                    sa.String(512),
                    nullable=False,
                    server_default="",
                ),
            )
    if "control_user_mfa" not in tables:
        _create_user_mfa()
    if "control_mfa_challenges" not in tables:
        _create_mfa_challenges()
    if "control_approvals" not in tables:
        _create_approvals()
    _set_version("12")


def downgrade() -> None:
    # 身份绑定与审批记录是事实数据；回退不删表。
    _set_version("11")
