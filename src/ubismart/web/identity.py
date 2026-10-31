"""RFC-0004 身份/审批后端的装配。

前置条件不满足时显式失败（fail-closed），不静默降级。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ubismart.config import Settings


@dataclass(frozen=True)
class IdentityStores:
    oidc_client: Any = None
    oidc_states: Any = None
    mfa_store: Any = None
    mfa_challenges: Any = None
    approval_store: Any = None


def build_identity_stores(
    settings: Settings,
    control_store: Any,
    account_store: Any,
    session_store: Any,
) -> IdentityStores:
    """构造 RFC-0004 的身份/审批后端；前置条件不满足时显式失败。"""
    oidc_client = None
    oidc_states = None
    mfa_store = None
    mfa_challenges = None
    approval_store = None
    if settings.oidc_enabled:
        if (
            control_store is None
            or account_store is None
            or session_store is None
        ):
            raise RuntimeError(
                "AGENT_OIDC_ENABLED=true 需要企业账号与控制面数据库"
            )
        from ubismart.control.oidc import (
            OidcClient,
            OidcSettings,
            OidcStateStore,
        )

        try:
            oidc_client = OidcClient(OidcSettings.from_settings(settings))
        except Exception as exc:  # noqa: BLE001 - 配置错误要显式失败
            raise RuntimeError(f"OIDC 配置无效：{exc}") from exc
        oidc_states = OidcStateStore(control_store.engine)
    if settings.mfa_enabled:
        if control_store is None or account_store is None:
            raise RuntimeError(
                "AGENT_MFA_ENABLED=true 需要企业账号与控制面数据库"
            )
        from ubismart.control.mfa import (
            MfaChallengeStore,
            MfaStore,
        )

        try:
            mfa_store = MfaStore.from_settings(
                control_store.engine, settings
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"MFA 配置无效：{exc}") from exc
        mfa_challenges = MfaChallengeStore(control_store.engine)
    if settings.approval_four_eyes:
        if control_store is None or account_store is None:
            raise RuntimeError(
                "AGENT_APPROVAL_FOUR_EYES=true 需要企业账号与控制面数据库"
            )
        from ubismart.control.approvals import ApprovalStore

        approval_store = ApprovalStore(control_store.engine)
    return IdentityStores(
        oidc_client=oidc_client,
        oidc_states=oidc_states,
        mfa_store=mfa_store,
        mfa_challenges=mfa_challenges,
        approval_store=approval_store,
    )
