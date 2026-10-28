"""企业账号、密码哈希与会话存储。"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.control.accounts import (
    AccountError,
    SessionStore,
    UserStore,
    hash_password,
    verify_password,
)
from coding_agent.control.store import ControlStore


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


def test_short_password_is_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AccountError, match="至少 8 位"):
        UserStore(store.engine).create("carol", "short")
    store.close()
