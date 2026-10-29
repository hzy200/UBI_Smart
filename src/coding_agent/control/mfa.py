"""TOTP MFA（RFC-0004）。

- 密钥用主密钥 AES-256-GCM 加密，AAD 绑定 `(tenant, user)`；
- 恢复码只存 SHA-256，使用后删除；
- 同一 TOTP 时间步只能成功一次（`last_used_step` 条件更新）；
- 登录挑战只存 token 哈希，失败次数有上限。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from coding_agent.control.credentials import load_master_key
from coding_agent.control.schema import (
    ControlMfaChallenge,
    ControlUserMfa,
)

_TOTP_STEP_S = 30
_TOTP_DIGITS = 6


class MfaError(RuntimeError):
    pass


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp(secret: str, counter: int) -> str:
    padded = secret + "=" * (-len(secret) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except (ValueError, TypeError) as exc:
        raise MfaError("TOTP secret 非法") from exc
    digest = hmac.new(
        key, struct.pack(">Q", counter), hashlib.sha1
    ).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0]
    return f"{binary & 0x7FFFFFFF:0{_TOTP_DIGITS}d}"[-_TOTP_DIGITS:]


def verify_totp(
    secret: str,
    code: str,
    *,
    window: int = 1,
    last_step: int = 0,
    now: int | None = None,
) -> int | None:
    """返回命中的时间步；失败/重放返回 None。"""
    text = str(code or "").replace(" ", "").strip()
    if len(text) != _TOTP_DIGITS or not text.isdigit():
        return None
    current = int(time.time() if now is None else now) // _TOTP_STEP_S
    for step in range(current - max(0, window), current + max(0, window) + 1):
        if step <= int(last_step or 0):
            continue
        if hmac.compare_digest(_totp(secret, step), text):
            return step
    return None


def otpauth_uri(issuer: str, account: str, secret: str) -> str:
    label = f"{issuer}:{account}"
    from urllib.parse import quote

    return (
        f"otpauth://totp/{quote(label, safe=':')}"
        f"?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={_TOTP_DIGITS}&period={_TOTP_STEP_S}"
    )


def _aad(tenant: str, user_id: str) -> bytes:
    return f"mfa|{tenant}|{user_id}".encode()


def _encrypt(key: bytes, tenant: str, user_id: str, secret: str) -> tuple[str, str]:
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(key).encrypt(
        nonce, secret.encode("utf-8"), _aad(tenant, user_id)
    )
    return (
        base64.b64encode(cipher).decode("ascii"),
        base64.b64encode(nonce).decode("ascii"),
    )


def _decrypt(
    key: bytes, tenant: str, user_id: str, ciphertext: str, nonce: str
) -> str:
    try:
        plain = AESGCM(key).decrypt(
            base64.b64decode(nonce),
            base64.b64decode(ciphertext),
            _aad(tenant, user_id),
        )
    except Exception as exc:  # noqa: BLE001 - 解密失败 fail closed
        raise MfaError("TOTP 密钥解密失败；请检查主密钥") from exc
    return plain.decode("utf-8")


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(str(code).strip().upper().encode("utf-8")).hexdigest()


def generate_recovery_codes(count: int = 8) -> list[str]:
    return [
        "-".join(
            secrets.token_hex(2).upper()
            for _ in range(2)
        )
        for _ in range(max(1, int(count)))
    ]


class MfaStore:
    def __init__(self, engine: Engine, key: bytes, *, key_version: int = 1) -> None:
        self.engine = engine
        self.key = key
        self.key_version = max(1, int(key_version or 1))

    @classmethod
    def from_settings(cls, engine: Engine, settings: Any) -> MfaStore:
        key = load_master_key(
            getattr(settings, "secret_master_key", ""),
            getattr(settings, "secret_master_key_file", ""),
        )
        if not key:
            raise MfaError(
                "启用 MFA 需要 AGENT_SECRET_MASTER_KEY 或 _FILE"
            )
        return cls(
            engine,
            key,
            key_version=int(
                getattr(settings, "secret_master_key_version", 1) or 1
            ),
        )

    def status(self, user_id: str) -> dict[str, Any]:
        with Session(self.engine) as session:
            row = session.get(ControlUserMfa, str(user_id))
            if row is None:
                return {"enabled": False, "recovery_codes": 0}
            try:
                codes = json.loads(row.recovery_hashes or "[]")
            except ValueError:
                codes = []
            return {
                "enabled": bool(row.enabled),
                "recovery_codes": len(codes) if isinstance(codes, list) else 0,
            }

    def _row(self, session: Session, user_id: str) -> ControlUserMfa | None:
        return session.get(ControlUserMfa, str(user_id))

    def enroll(self, *, tenant: str, user_id: str) -> str:
        """生成新密钥并保存为未启用；重复调用会覆盖旧待确认密钥。"""
        secret = generate_secret()
        ciphertext, nonce = _encrypt(
            self.key, tenant, str(user_id), secret
        )
        now = str(int(time.time()))
        with Session(self.engine) as session, session.begin():
            row = self._row(session, user_id)
            if row is None:
                row = ControlUserMfa(
                    user_id=str(user_id), created_at=now
                )
                session.add(row)
            row.secret_ciphertext = ciphertext
            row.nonce = nonce
            row.key_version = self.key_version
            row.enabled = False
            row.recovery_hashes = "[]"
            row.last_used_step = 0
            row.updated_at = now
        return secret

    def confirm(
        self,
        *,
        tenant: str,
        user_id: str,
        code: str,
        window: int = 1,
    ) -> list[str]:
        with Session(self.engine) as session, session.begin():
            row = self._row(session, user_id)
            if row is None or not row.secret_ciphertext:
                raise MfaError("没有待确认的 TOTP 密钥")
            secret = _decrypt(
                self.key, tenant, str(user_id), row.secret_ciphertext, row.nonce
            )
            step = verify_totp(secret, code, window=window)
            if step is None:
                raise MfaError("动态码不正确")
            codes = generate_recovery_codes()
            row.enabled = True
            row.last_used_step = step
            row.recovery_hashes = json.dumps(
                [hash_recovery_code(item) for item in codes]
            )
            row.updated_at = str(int(time.time()))
            return codes

    def verify(
        self,
        *,
        tenant: str,
        user_id: str,
        code: str,
        window: int = 1,
    ) -> bool:
        with Session(self.engine) as session, session.begin():
            row = self._row(session, user_id)
            if row is None or not row.enabled or not row.secret_ciphertext:
                return False
            secret = _decrypt(
                self.key, tenant, str(user_id), row.secret_ciphertext, row.nonce
            )
            step = verify_totp(
                secret, code, window=window, last_step=row.last_used_step
            )
            if step is None:
                return False
            updated = session.execute(
                update(ControlUserMfa)
                .where(
                    ControlUserMfa.user_id == str(user_id),
                    ControlUserMfa.last_used_step < step,
                )
                .values(last_used_step=step)
            ).rowcount
            return bool(updated)

    def verify_recovery(
        self, *, user_id: str, code: str
    ) -> bool:
        digest = hash_recovery_code(code)
        with Session(self.engine) as session, session.begin():
            row = self._row(session, user_id)
            if row is None or not row.enabled:
                return False
            try:
                hashes = json.loads(row.recovery_hashes or "[]")
            except ValueError:
                hashes = []
            if not isinstance(hashes, list):
                return False
            matched = any(
                hmac.compare_digest(digest, str(item)) for item in hashes
            )
            if not matched:
                return False
            row.recovery_hashes = json.dumps(
                [item for item in hashes if str(item) != digest]
            )
            row.updated_at = str(int(time.time()))
            return True

    def disable(self, user_id: str) -> None:
        with Session(self.engine) as session, session.begin():
            row = self._row(session, user_id)
            if row is not None:
                row.enabled = False
                row.secret_ciphertext = ""
                row.nonce = ""
                row.recovery_hashes = "[]"
                row.last_used_step = 0
                row.updated_at = str(int(time.time()))

    def reset(self, user_id: str) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                delete(ControlUserMfa).where(
                    ControlUserMfa.user_id == str(user_id)
                )
            )


class MfaChallengeStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def create(self, user_id: str, *, ttl_s: int = 300) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with Session(self.engine) as session, session.begin():
            session.add(
                ControlMfaChallenge(
                    token_hash=self._hash(token),
                    user_id=str(user_id),
                    attempts=0,
                    created_at=str(now),
                    expires_at=str(now + max(30, int(ttl_s))),
                )
            )
        return token

    def get(self, token: str) -> dict[str, Any] | None:
        now = int(time.time())
        with Session(self.engine) as session:
            row = session.get(
                ControlMfaChallenge, self._hash(token)
            )
            if row is None or int(row.expires_at or 0) < now:
                return None
            return {
                "user_id": row.user_id,
                "attempts": int(row.attempts or 0),
                "expires_at": int(row.expires_at or 0),
            }

    def fail(self, token: str) -> int:
        digest = self._hash(token)
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlMfaChallenge, digest)
            if row is None:
                return 0
            row.attempts = int(row.attempts or 0) + 1
            session.flush()
            return row.attempts

    def delete(self, token: str) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                delete(ControlMfaChallenge).where(
                    ControlMfaChallenge.token_hash == self._hash(token)
                )
            )

    def purge_expired(self) -> int:
        now = int(time.time())
        with Session(self.engine) as session, session.begin():
            return int(
                session.execute(
                    delete(ControlMfaChallenge).where(
                        ControlMfaChallenge.expires_at < str(now)
                    )
                ).rowcount
                or 0
            )
