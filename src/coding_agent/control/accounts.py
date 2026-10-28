"""企业账号与会话存储。

密码只存 scrypt 哈希；会话只存 token 的 sha256。两者都不写日志、不回显。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from coding_agent.control.schema import (
    ControlAuthEvent,
    ControlAuthSession,
    ControlUser,
)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


class AccountError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _future(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def hash_password(password: str) -> str:
    raw = str(password or "")
    if len(raw) < 8:
        raise AccountError("密码至少 8 位")
    if len(raw) > 200:
        raise AccountError("密码过长")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        raw.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = str(encoded or "").split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _public(row: ControlUser) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant": row.tenant,
        "username": row.username,
        "email": row.email,
        "role": row.role,
        "status": row.status,
        "created_at": row.created_at,
        "last_login_at": row.last_login_at,
    }


class UserStore:
    """账号 CRUD；所有写操作显式提交，读操作返回普通 dict。"""

    def __init__(self, engine: Any, *, tenant: str = "default") -> None:
        self.engine = engine
        self.tenant = str(tenant or "default")

    def _validate_username(self, username: str) -> str:
        text = str(username or "").strip()
        if not _USERNAME_RE.match(text) or text in {".", ".."}:
            raise AccountError("用户名只允许字母、数字、下划线、点、连字符（1–64）")
        return text

    def create(
        self,
        username: str,
        password: str,
        *,
        email: str = "",
        role: str = "member",
        tenant: str | None = None,
    ) -> dict[str, Any]:
        name = self._validate_username(username)
        if role not in {"owner", "admin", "member"}:
            raise AccountError("非法角色")
        scope = str(tenant or self.tenant)
        now = _now()
        row = ControlUser(
            id=secrets.token_hex(18),
            tenant=scope,
            username=name,
            email=str(email or "")[:255],
            role=role,
            password_hash=hash_password(password),
            status="active",
            created_at=now,
            updated_at=now,
        )
        with Session(self.engine) as session, session.begin():
            exists = session.scalar(
                select(ControlUser.id).where(
                    ControlUser.tenant == scope,
                    ControlUser.username == name,
                )
            )
            if exists:
                raise AccountError("用户名已存在")
            session.add(row)
            session.flush()
            result = _public(row)
        return result

    def list(self, tenant: str | None = None) -> list[dict[str, Any]]:
        scope = str(tenant or self.tenant)
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlUser)
                .where(ControlUser.tenant == scope)
                .order_by(ControlUser.created_at, ControlUser.username)
            ).all()
            return [_public(row) for row in rows]

    def get(self, user_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.get(ControlUser, str(user_id))
            return _public(row) if row is not None else None

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.scalar(
                select(ControlUser).where(
                    ControlUser.tenant == self.tenant,
                    ControlUser.username == str(username or ""),
                )
            )
            return _public(row) if row is not None else None

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(ControlUser).where(
                    ControlUser.tenant == self.tenant,
                    ControlUser.username == str(username or ""),
                )
            )
            if row is None or row.status != "active":
                return None
            if not verify_password(password, row.password_hash):
                return None
            row.last_login_at = _now()
            row.updated_at = row.last_login_at
            session.flush()
            return _public(row)

    def update(
        self,
        user_id: str,
        *,
        status: str | None = None,
        role: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlUser, str(user_id))
            if row is None:
                raise AccountError("用户不存在")
            if status is not None:
                if status not in {"active", "disabled"}:
                    raise AccountError("非法状态")
                row.status = status
            if role is not None:
                if role not in {"owner", "admin", "member"}:
                    raise AccountError("非法角色")
                row.role = role
            if email is not None:
                row.email = str(email)[:255]
            row.updated_at = _now()
            session.flush()
            return _public(row)

    def reset_password(self, user_id: str, password: str) -> None:
        encoded = hash_password(password)
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlUser, str(user_id))
            if row is None:
                raise AccountError("用户不存在")
            row.password_hash = encoded
            row.updated_at = _now()

    def user_from_id(self, user_id: str) -> dict[str, Any] | None:
        return self.get(user_id)

    def count_active_owners(self, tenant: str | None = None) -> int:
        scope = str(tenant or self.tenant)
        with Session(self.engine) as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(ControlUser)
                    .where(
                        ControlUser.tenant == scope,
                        ControlUser.role == "owner",
                        ControlUser.status == "active",
                    )
                )
                or 0
            )


class AuthEventStore:
    """登录安全事件；只记录身份与来源，不记录密码/token。"""

    def __init__(self, engine: Any, *, tenant: str = "default") -> None:
        self.engine = engine
        self.tenant = str(tenant or "default")

    def record(
        self,
        username: str,
        event: str,
        *,
        ip: str = "",
        request_id: str = "",
        tenant: str | None = None,
    ) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(
                ControlAuthEvent(
                    id=secrets.token_hex(18),
                    tenant=str(tenant or self.tenant),
                    username=str(username or "")[:64],
                    event=str(event or "")[:32],
                    ip=str(ip or "")[:64],
                    request_id=str(request_id or "")[:64],
                    ts=_now(),
                )
            )

    def list_recent(
        self,
        *,
        username: str = "",
        tenant: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        scope = str(tenant or self.tenant)
        query = select(ControlAuthEvent).where(
            ControlAuthEvent.tenant == scope
        )
        if username:
            query = query.where(
                ControlAuthEvent.username == str(username)
            )
        query = query.order_by(ControlAuthEvent.ts.desc()).limit(
            max(1, min(200, int(limit or 50)))
        )
        with Session(self.engine) as session:
            rows = session.scalars(query).all()
        return [
            {
                "id": row.id,
                "username": row.username,
                "event": row.event,
                "ip": row.ip,
                "request_id": row.request_id,
                "ts": row.ts,
            }
            for row in rows
        ]


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class SessionStore:
    """登录会话；token 明文只返回给客户端，服务端只存哈希。"""

    def __init__(
        self,
        engine: Any,
        *,
        ttl_s: int = 12 * 3600,
        cookie_name: str = "ca_session",
        csrf_cookie: str = "ca_csrf",
    ) -> None:
        self.engine = engine
        self.ttl_s = max(300, int(ttl_s))
        self.cookie_name = cookie_name
        self.csrf_cookie = csrf_cookie

    def create(self, user_id: str) -> dict[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with Session(self.engine) as session, session.begin():
            session.add(
                ControlAuthSession(
                    token_hash=_token_hash(token),
                    user_id=str(user_id),
                    csrf_secret=csrf,
                    expires_at=_future(self.ttl_s),
                    revoked_at="",
                    created_at=_now(),
                )
            )
        return {"token": token, "csrf": csrf, "expires_at": _future(self.ttl_s)}

    def resolve(self, token: str) -> tuple[dict[str, Any], str] | None:
        if not token:
            return None
        with Session(self.engine) as session:
            # 单次 join：Cookie 认证在每次请求都会走这里，省一次往返。
            row = session.execute(
                select(ControlAuthSession, ControlUser)
                .join(
                    ControlUser,
                    ControlUser.id == ControlAuthSession.user_id,
                )
                .where(
                    ControlAuthSession.token_hash == _token_hash(token)
                )
            ).first()
            if row is None:
                return None
            session_row, user = row
            if session_row.revoked_at:
                return None
            if (
                session_row.expires_at
                and session_row.expires_at <= _now()
            ):
                return None
            if user.status != "active":
                return None
            return _public(user), session_row.csrf_secret

    def revoke(self, token: str) -> None:
        if not token:
            return
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlAuthSession, _token_hash(token))
            if row is not None and not row.revoked_at:
                row.revoked_at = _now()

    def purge_expired(self) -> int:
        """清理过期会话；登录时顺带执行，靠 expires_at 索引。"""
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                delete(ControlAuthSession).where(
                    ControlAuthSession.expires_at != "",
                    ControlAuthSession.expires_at <= _now(),
                )
            )
            return int(result.rowcount or 0)

    def revoke_user(self, user_id: str) -> None:
        with Session(self.engine) as session, session.begin():
            rows = session.scalars(
                select(ControlAuthSession).where(
                    ControlAuthSession.user_id == str(user_id),
                    ControlAuthSession.revoked_at == "",
                )
            ).all()
            now = _now()
            for row in rows:
                row.revoked_at = now
