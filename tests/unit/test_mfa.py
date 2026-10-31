"""RFC-0004：TOTP、恢复码、挑战与加密存储。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import ubismart.control.mfa as mfa_module
from ubismart.control.mfa import (
    MfaChallengeStore,
    MfaError,
    MfaStore,
    _decrypt,
    _totp,
    generate_secret,
    verify_totp,
)
from ubismart.control.schema import ControlUserMfa
from ubismart.control.store import ControlStore

KEY = b"k" * 32
NOW = (1_700_000_000 // 30) * 30


def _store(tmp_path: Path) -> ControlStore:
    return ControlStore(f"sqlite:///{tmp_path / 'control.db'}")


def _code(store: MfaStore, user_id: str, *, tenant: str = "default") -> str:
    with Session(store.engine) as session:
        row = session.get(ControlUserMfa, user_id)
        assert row is not None
        secret = _decrypt(
            KEY, tenant, user_id, row.secret_ciphertext, row.nonce
        )
    return _totp(secret, NOW // 30)


def test_totp_roundtrip_and_replay(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mfa_module.time, "time", lambda: NOW)
    secret = generate_secret()
    code = _totp(secret, NOW // 30)
    assert verify_totp(secret, code, now=NOW) == NOW // 30
    assert verify_totp(
        secret, code, now=NOW, last_step=NOW // 30
    ) is None
    assert verify_totp(secret, "000000", now=NOW) is None


def test_store_enroll_confirm_and_verify(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mfa_module.time, "time", lambda: NOW)
    store = _store(tmp_path)
    mfa = MfaStore(store.engine, KEY)
    mfa.enroll(tenant="default", user_id="u1")
    codes = mfa.confirm(
        tenant="default",
        user_id="u1",
        code=_code(mfa, "u1"),
        window=1,
    )
    assert len(codes) == 8
    assert mfa.status("u1") == {"enabled": True, "recovery_codes": 8}

    # 同一时间步不能重放
    assert (
        mfa.verify(
            tenant="default",
            user_id="u1",
            code=_code(mfa, "u1"),
        )
        is False
    )

    monkeypatch.setattr(mfa_module.time, "time", lambda: NOW + 30)
    with Session(store.engine) as session:
        row = session.get(ControlUserMfa, "u1")
        assert row is not None
        secret = _decrypt(
            KEY, "default", "u1", row.secret_ciphertext, row.nonce
        )
    assert mfa.verify(
        tenant="default",
        user_id="u1",
        code=_totp(secret, (NOW + 30) // 30),
    )
    store.close()


def test_recovery_code_is_single_use(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mfa_module.time, "time", lambda: NOW)
    store = _store(tmp_path)
    mfa = MfaStore(store.engine, KEY)
    mfa.enroll(tenant="default", user_id="u1")
    codes = mfa.confirm(
        tenant="default", user_id="u1", code=_code(mfa, "u1")
    )
    assert mfa.verify_recovery(user_id="u1", code=codes[0]) is True
    assert mfa.verify_recovery(user_id="u1", code=codes[0]) is False
    assert mfa.status("u1")["recovery_codes"] == 7
    store.close()


def test_disable_and_wrong_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mfa_module.time, "time", lambda: NOW)
    store = _store(tmp_path)
    mfa = MfaStore(store.engine, KEY)
    mfa.enroll(tenant="default", user_id="u1")
    mfa.confirm(
        tenant="default", user_id="u1", code=_code(mfa, "u1")
    )
    wrong = MfaStore(store.engine, b"x" * 32)
    with pytest.raises(MfaError):
        wrong.verify(
            tenant="default",
            user_id="u1",
            code=_code(mfa, "u1"),
        )
    mfa.disable("u1")
    assert mfa.status("u1")["enabled"] is False
    store.close()


def test_challenge_attempts_and_purge(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mfa_module.time, "time", lambda: NOW)
    store = _store(tmp_path)
    challenges = MfaChallengeStore(store.engine)
    token = challenges.create("u1", ttl_s=60)
    assert challenges.get(token)["user_id"] == "u1"
    for expected in range(1, 4):
        assert challenges.fail(token) == expected
    challenges.delete(token)
    assert challenges.get(token) is None

    token = challenges.create("u1", ttl_s=60)
    monkeypatch.setattr(
        mfa_module.time, "time", lambda: NOW + 3600
    )
    assert challenges.purge_expired() == 1
    assert challenges.get(token) is None
    store.close()
