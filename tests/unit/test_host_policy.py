"""A2L 授权与 ZX：判定顺序、TTL/撤销、敏感根。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from coding_agent.host_broker.policy import (
    DENIED_SCOPE,
    DENIED_VERB,
    DENIED_ZX,
    GRANT_EXPIRED,
    GRANT_REQUIRED,
    GRANT_REVOKED,
    SENSITIVE_ROOT,
    GrantStore,
    HostPolicy,
    normalize_scope,
)


def _policy(tmp_path):
    return HostPolicy(
        deny_roots=[str(tmp_path / "Windows")],
        sensitive_roots=[str(tmp_path / "Documents")],
    )


def _grant_for(policy, path, *, verbs=("read",), sensitive=False):
    store = GrantStore()
    grant = store.issue(
        scope=[str(path)],
        verbs=verbs,
        ttl_s=1800,
        reason="test",
        sensitive=sensitive,
    )
    return store, grant


def test_zx_wins_over_grant(tmp_path) -> None:
    policy = _policy(tmp_path)
    store, grant = _grant_for(policy, tmp_path)
    target = tmp_path / "Windows" / "secret.txt"
    decision = policy.check(str(target), "read", grant)
    assert decision.allowed is False
    assert decision.code == DENIED_ZX


def test_missing_grant_requires_authorization(tmp_path) -> None:
    policy = _policy(tmp_path)
    decision = policy.check(str(tmp_path / "a.py"), "read", None)
    assert decision.code == GRANT_REQUIRED


def test_scope_and_verb_are_enforced(tmp_path) -> None:
    policy = _policy(tmp_path)
    project = tmp_path / "project"
    store, grant = _grant_for(policy, project, verbs={"read"})
    assert policy.check(str(project / "a.py"), "read", grant).allowed is True

    outside = policy.check(str(tmp_path / "other" / "a.py"), "read", grant)
    assert outside.code == DENIED_SCOPE

    write = policy.check(str(project / "a.py"), "write", grant)
    assert write.code == DENIED_VERB


def test_expired_and_revoked_are_distinguished(tmp_path) -> None:
    policy = _policy(tmp_path)
    project = tmp_path / "project"
    store, grant = _grant_for(policy, project)
    later = datetime.now(UTC) + timedelta(seconds=1801)
    assert policy.check(str(project / "a.py"), "read", grant, now=later).code == GRANT_EXPIRED

    store.revoke(grant.grant_id)
    revoked = store.get(grant.grant_id)
    assert revoked is not None
    assert policy.check(str(project / "a.py"), "read", revoked).code == GRANT_REVOKED


def test_sensitive_root_needs_ack_or_sensitive_grant(tmp_path) -> None:
    policy = _policy(tmp_path)
    documents = tmp_path / "Documents"
    store, grant = _grant_for(policy, documents)
    target = str(documents / "a.txt")
    assert policy.check(target, "read", grant).code == SENSITIVE_ROOT
    assert policy.check(target, "read", grant, sensitive_ack=True).allowed is True

    store2 = GrantStore()
    sensitive_grant = store2.issue(
        scope=[str(documents)], verbs=["read"], sensitive=True
    )
    assert policy.check(target, "read", sensitive_grant).allowed is True


def test_grant_store_validates_scope_ttl_and_verbs(tmp_path) -> None:
    store = GrantStore()
    with pytest.raises(ValueError):
        store.issue(scope=["relative"], verbs=["read"])
    with pytest.raises(ValueError):
        store.issue(scope=[os.path.abspath(os.sep)], verbs=["read"])
    with pytest.raises(ValueError):
        store.issue(scope=[str(tmp_path)], verbs=["read"], ttl_s=0)
    with pytest.raises(ValueError):
        store.issue(scope=[str(tmp_path)], verbs=["fly"])


def test_one_shot_grant_can_only_be_claimed_once(tmp_path) -> None:
    store = GrantStore()
    grant = store.issue(
        scope=[str(tmp_path)], verbs=["read"], one_shot=True
    )
    assert store.claim(grant.grant_id) is True
    assert store.claim(grant.grant_id) is False
    current = store.get(grant.grant_id)
    assert current is not None and current.used is True


def test_claim_rejects_revoked_and_expired(tmp_path) -> None:
    store = GrantStore()
    revoked = store.issue(
        scope=[str(tmp_path)], verbs=["read"], one_shot=True
    )
    store.revoke(revoked.grant_id)
    assert store.claim(revoked.grant_id) is False

    expired = store.issue(
        scope=[str(tmp_path)], verbs=["read"], one_shot=True
    )
    later = datetime.now(UTC) + timedelta(seconds=3600)
    assert store.claim(expired.grant_id, now=later) is False


def test_normalize_scope_rejects_home_root() -> None:
    with pytest.raises(ValueError):
        normalize_scope(os.path.expanduser("~"))


def test_from_settings_keeps_defaults_and_adds_extras(tmp_path) -> None:
    from coding_agent.config import Settings

    extra = tmp_path / "extra"
    settings = Settings(
        _env_file=None,
        AGENT_HOST_DENY_ROOTS=str(extra),
        AGENT_HOST_SENSITIVE_ROOTS=str(tmp_path / "Sensitive"),
    )
    policy = HostPolicy.from_settings(settings)
    assert str(extra) in policy.deny_roots
    expected_default = r"C:\Windows" if os.name == "nt" else "/etc"
    assert any(
        os.path.normcase(os.path.normpath(root))
        == os.path.normcase(os.path.normpath(expected_default))
        for root in policy.deny_roots
    )
    assert str(tmp_path / "Sensitive") in policy.sensitive_roots
