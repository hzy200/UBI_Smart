"""企业账号、密码哈希与会话存储。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ubismart.control.accounts import (
    AccountError,
    AuthEventStore,
    PasswordResetStore,
    SessionStore,
    UserStore,
    hash_password,
    verify_password,
)
from ubismart.control.store import ControlStore


def _store(tmp_path: Path) -> ControlStore:
    return ControlStore(f"sqlite:///{tmp_path / 'control.db'}")


def test_scrypt_password_roundtrip() -> None:
    encoded = hash_password("correct horse")
    assert encoded.startswith("scrypt$")
    assert "correct horse" not in encoded
    assert verify_password("correct horse", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("correct horse", "broken")


def test_create_and_authenticate_user(tmp_path) -> None:
    store = _store(tmp_path)
    users = UserStore(store.engine, tenant="default")
    row = users.create("alice", "password-123", email="a@example.com")
    assert row["username"] == "alice"
    assert row["role"] == "member"
    assert users.authenticate("alice", "password-123")["username"] == "alice"
    assert users.authenticate("alice", "bad") is None
    with pytest.raises(AccountError, match="已存在"):
        users.create("alice", "password-123")
    users.update(row["id"], status="disabled")
    assert users.authenticate("alice", "password-123") is None
    store.close()


def test_oidc_identity_mapping_is_stable_and_unique(tmp_path) -> None:
    store = _store(tmp_path)
    users = UserStore(store.engine, tenant="default")
    first = users.find_or_create_oidc(
        issuer="https://idp",
        subject="subject-1",
        username="alice",
        email="alice@example.com",
        role="admin",
    )
    assert first is not None
    assert first["username"] == "alice"
    assert first["role"] == "admin"

    again = users.find_or_create_oidc(
        issuer="https://idp",
        subject="subject-1",
        username="changed",
        email="new@example.com",
    )
    assert again["id"] == first["id"]
    assert users.authenticate("alice", "") is None  # OIDC-only 无本地密码

    other = users.find_or_create_oidc(
        issuer="https://idp",
        subject="subject-2",
        username="alice",
    )
    assert other["id"] != first["id"]
    assert other["username"] != "alice"

    missing = users.find_or_create_oidc(
        issuer="https://idp",
        subject="subject-3",
        username="bob",
        auto_create=False,
    )
    assert missing is None

    users.update(first["id"], status="disabled")
    assert (
        users.find_or_create_oidc(
            issuer="https://idp",
            subject="subject-1",
            username="alice",
        )
        is None
    )
    store.close()


def test_session_lifecycle_and_revoke(tmp_path) -> None:
    store = _store(tmp_path)
    users = UserStore(store.engine)
    user = users.create("bob", "password-123")
    sessions = SessionStore(store.engine, ttl_s=3600)
    created = sessions.create(user["id"])
    resolved = sessions.resolve(created["token"])
    assert resolved is not None
    assert resolved[0]["username"] == "bob"
    assert resolved[1] == created["csrf"]

    sessions.revoke(created["token"])
    assert sessions.resolve(created["token"]) is None

    second = sessions.create(user["id"])
    sessions.revoke_user(user["id"])
    assert sessions.resolve(second["token"]) is None
    store.close()


def test_list_and_revoke_other_sessions(tmp_path) -> None:
    store = _store(tmp_path)
    users = UserStore(store.engine)
    user = users.create("bob", "password-123")
    sessions = SessionStore(store.engine, ttl_s=3600)
    first = sessions.create(user["id"])
    sessions.create(user["id"])
    sessions.create(user["id"])

    rows = sessions.list_for_user(
        user["id"], current_token=first["token"]
    )
    assert len(rows) == 3
    assert sum(1 for row in rows if row["current"]) == 1
    assert all("token" not in row for row in rows)

    assert sessions.revoke_others(
        user["id"], keep_token=first["token"]
    ) == 2
    rows = sessions.list_for_user(
        user["id"], current_token=first["token"]
    )
    assert len(rows) == 1 and rows[0]["current"] is True
    assert sessions.resolve(first["token"]) is not None

    # 不保留当前会话时全部登出。
    assert sessions.revoke_others(user["id"]) == 1
    assert sessions.resolve(first["token"]) is None
    store.close()


def test_short_password_is_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AccountError, match="至少 8 位"):
        UserStore(store.engine).create("carol", "short")
    store.close()


def test_unknown_and_disabled_accounts_still_pay_scrypt(
    tmp_path, monkeypatch
) -> None:
    import ubismart.control.accounts as accounts

    calls: list[str] = []
    real = accounts.verify_password

    def spy(password: str, encoded: str) -> bool:
        calls.append(encoded)
        return real(password, encoded)

    monkeypatch.setattr(accounts, "verify_password", spy)
    store = _store(tmp_path)
    users = UserStore(store.engine, tenant="default")
    row = users.create("alice", "password-123")

    assert users.authenticate("ghost", "password-123") is None
    assert len(calls) == 1 and calls[0]

    users.update(row["id"], status="disabled")
    assert users.authenticate("alice", "password-123") is None
    assert len(calls) == 2
    assert calls[1] and calls[1] != calls[0]

    # OIDC 用户没有本地密码哈希时也要走一次 dummy scrypt。
    users.find_or_create_oidc(
        issuer="https://idp.example",
        subject="s1",
        username="sso",
    )
    assert users.authenticate("sso", "password-123") is None
    assert len(calls) == 3 and calls[2]
    store.close()


def test_recent_failures_reset_after_success(tmp_path) -> None:
    store = _store(tmp_path)
    events = AuthEventStore(store.engine, tenant="default")
    for _ in range(3):
        events.record("alice", "login_failed")
    assert events.recent_failures("alice") == 3
    events.record("alice", "login_ok")
    assert events.recent_failures("alice") == 0
    events.record("alice", "login_failed")
    assert events.recent_failures("alice") == 1
    assert events.recent_failures("") == 0
    store.close()


def test_session_idle_timeout_renewal_and_absolute_cap(
    tmp_path, monkeypatch
) -> None:
    from sqlalchemy.orm import Session

    import ubismart.control.accounts as accounts
    from ubismart.control.schema import ControlAuthSession

    store = _store(tmp_path)
    users = UserStore(store.engine)
    user = users.create("bob", "password-123")
    sessions = SessionStore(
        store.engine, ttl_s=3600, idle_ttl_s=600
    )
    created = sessions.create(user["id"])
    expires = datetime.fromisoformat(created["expires_at"])
    assert 590 <= (expires - datetime.now(UTC)).total_seconds() <= 600

    # 人为把截止时间压到 10 秒后；一次活动应顺延到约 10 分钟。
    row = sessions.resolve(created["token"])
    assert row is not None
    with Session(store.engine) as session, session.begin():
        stored = session.get(ControlAuthSession, accounts._token_hash(
            created["token"]
        ))
        stored.expires_at = (
            datetime.now(UTC) + timedelta(seconds=10)
        ).isoformat(timespec="seconds")
        old = stored.expires_at
    assert sessions.resolve(created["token"]) is not None
    with Session(store.engine) as session:
        stored = session.get(ControlAuthSession, accounts._token_hash(
            created["token"]
        ))
        assert stored.expires_at > old

    # 绝对 TTL 上限：created + 3600，不会被空闲顺延突破。
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    cap = sessions._deadline(
        created_at,
        datetime.now(UTC) + timedelta(seconds=7200),
    )
    cap_delta = (
        datetime.fromisoformat(cap) - datetime.fromisoformat(created_at)
    ).total_seconds()
    assert 3590 <= cap_delta <= 3600
    monkeypatch.setattr(
        accounts,
        "_now",
        lambda: (
            datetime.now(UTC) + timedelta(hours=2)
        ).isoformat(timespec="seconds"),
    )
    assert sessions.resolve(created["token"]) is None
    store.close()


def test_password_reset_token_is_single_use(tmp_path, monkeypatch) -> None:
    import ubismart.control.accounts as accounts

    store = _store(tmp_path)
    users = UserStore(store.engine)
    user = users.create("bob", "password-123")
    resets = PasswordResetStore(store.engine, ttl_s=60)
    token = resets.create(user["id"])
    assert resets.consume(token, "new-password-123") == user["id"]
    assert users.authenticate("bob", "password-123") is None
    assert users.authenticate("bob", "new-password-123")["username"] == "bob"
    assert resets.consume(token, "another-password") is None

    # 过期令牌不可用。
    token = resets.create(user["id"])
    real_now = accounts._now
    monkeypatch.setattr(
        accounts,
        "_now",
        lambda: (
            datetime.now(UTC) + timedelta(hours=2)
        ).isoformat(timespec="seconds"),
    )
    assert resets.consume(token, "third-password-123") is None
    assert resets.purge_expired() >= 1
    monkeypatch.setattr(accounts, "_now", real_now)
    store.close()
