"""企业账号与会话存储。

密码只存 scrypt 哈希；会话只存 token 的 sha256。两者都不写日志、不回显。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coding_agent.access import ACCESS_MODES, ACCESS_WRITE_AUTO
from coding_agent.control.schema import (
    ControlAuthEvent,
    ControlAuthSession,
    ControlPasswordReset,
    ControlUser,
    ControlUserIdentity,
)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# owner = 唯一管理所有权；admin = 日常管理；approver = 只做四眼审批；
# auditor = 只读审计；member = 普通开发者。角色只影响控制面，不影响沙箱权限。
ACCOUNT_ROLES = frozenset(
    {"owner", "admin", "approver", "auditor", "member"}
)
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
# 未知/禁用账号也要付出与真实校验相近的 scrypt 成本，避免用响应时间
# 枚举用户名。惰性生成，避免每次 import 都算一遍。
_DUMMY_HASH = ""


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if not _DUMMY_HASH:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


class AccountError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_TS_LOCK = threading.Lock()
_LAST_TS_US = 0


def _now_us() -> str:
    """单调递增的微秒时间戳；失败锁定依赖同一时刻事件的先后。"""
    global _LAST_TS_US
    current = int(datetime.now(UTC).timestamp() * 1_000_000)
    with _TS_LOCK:
        _LAST_TS_US = max(current, _LAST_TS_US + 1)
        value = _LAST_TS_US
    return datetime.fromtimestamp(
        value / 1_000_000, UTC
    ).isoformat(timespec="microseconds")


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
        "max_access_mode": row.max_access_mode or ACCESS_WRITE_AUTO,
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
        max_access_mode: str = ACCESS_WRITE_AUTO,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        name = self._validate_username(username)
        if role not in ACCOUNT_ROLES:
            raise AccountError("非法角色")
        if max_access_mode not in ACCESS_MODES:
            raise AccountError("非法访问上限")
        scope = str(tenant or self.tenant)
        now = _now()
        row = ControlUser(
            id=secrets.token_hex(18),
            tenant=scope,
            username=name,
            email=str(email or "")[:255],
            role=role,
            max_access_mode=max_access_mode,
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

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        target = str(email or "").strip().lower()
        if not target:
            return None
        with Session(self.engine) as session:
            row = session.scalar(
                select(ControlUser).where(
                    ControlUser.tenant == self.tenant,
                    func.lower(ControlUser.email) == target,
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
            if (
                row is None
                or row.status != "active"
                or not row.password_hash
            ):
                # 无论账号不存在、被禁用还是没有密码哈希，都执行一次
                # scrypt；否则攻击者可以用响应时间区分账号状态。
                verify_password(
                    password,
                    row.password_hash
                    if row is not None and row.password_hash
                    else _dummy_hash(),
                )
                return None
            if not verify_password(password, row.password_hash):
                return None
            row.last_login_at = _now()
            row.updated_at = row.last_login_at
            session.flush()
            return _public(row)

    def find_or_create_oidc(
        self,
        *,
        issuer: str,
        subject: str,
        username: str,
        email: str = "",
        role: str = "member",
        auto_create: bool = True,
    ) -> dict[str, Any] | None:
        """按 `(tenant, issuer, subject)` 绑定本地用户；禁用/不存在返回 None。"""
        tenant = str(self.tenant)
        issuer = str(issuer or "")[:255]
        subject = str(subject or "")[:255]
        if not issuer or not subject:
            return None
        role = role if role in ACCOUNT_ROLES else "member"
        for attempt in range(2):
            try:
                with Session(self.engine) as session, session.begin():
                    identity = session.scalar(
                        select(ControlUserIdentity).where(
                            ControlUserIdentity.tenant == tenant,
                            ControlUserIdentity.issuer == issuer,
                            ControlUserIdentity.subject == subject,
                        )
                    )
                    if identity is not None:
                        row = session.get(ControlUser, identity.user_id)
                        if row is None or row.status != "active":
                            return None
                        row.last_login_at = _now()
                        row.updated_at = row.last_login_at
                        if email and not row.email:
                            row.email = str(email)[:255]
                        identity.email = str(email or identity.email)[:255]
                        session.flush()
                        return _public(row)
                    if not auto_create:
                        return None
                    row = self._create_oidc_user(
                        session,
                        username=username,
                        email=email,
                        role=role,
                    )
                    session.add(
                        ControlUserIdentity(
                            id=secrets.token_hex(18),
                            tenant=tenant,
                            user_id=row.id,
                            issuer=issuer,
                            subject=subject,
                            email=str(email or "")[:255],
                            created_at=_now(),
                        )
                    )
                    session.flush()
                    return _public(row)
            except IntegrityError:
                # 并发创建同一个 identity / username：重试一次走查询路径。
                if attempt:
                    raise
                continue
        return None

    def _create_oidc_user(
        self,
        session: Session,
        *,
        username: str,
        email: str,
        role: str,
    ) -> ControlUser:
        base = re.sub(r"[^A-Za-z0-9_.-]", "-", str(username or ""))[:48]
        base = base.strip(".-") or f"user-{secrets.token_hex(4)}"
        if len(base) < 1:
            base = f"user-{secrets.token_hex(4)}"
        now = _now()
        for suffix in range(0, 20):
            candidate = base if suffix == 0 else f"{base}-{suffix + 1}"
            exists = session.scalar(
                select(ControlUser.id).where(
                    ControlUser.tenant == self.tenant,
                    ControlUser.username == candidate,
                )
            )
            if exists is None:
                row = ControlUser(
                    id=secrets.token_hex(18),
                    tenant=self.tenant,
                    username=candidate[:64],
                    email=str(email or "")[:255],
                    role=role,
                    password_hash="",
                    status="active",
                    created_at=now,
                    updated_at=now,
                    last_login_at=now,
                )
                session.add(row)
                session.flush()
                return row
        raise AccountError("无法为 OIDC 用户分配唯一用户名")

    def update(
        self,
        user_id: str,
        *,
        status: str | None = None,
        role: str | None = None,
        email: str | None = None,
        max_access_mode: str | None = None,
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
                if role not in ACCOUNT_ROLES:
                    raise AccountError("非法角色")
                row.role = role
            if email is not None:
                row.email = str(email)[:255]
            if max_access_mode is not None:
                if max_access_mode not in ACCESS_MODES:
                    raise AccountError("非法访问上限")
                row.max_access_mode = max_access_mode
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
                    ts=_now_us(),
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

    def recent_failures(
        self,
        username: str,
        *,
        window_s: int = 900,
        tenant: str | None = None,
    ) -> int:
        """统计窗口内、上次成功登录之后的失败次数（按账号退避用）。"""
        scope = str(tenant or self.tenant)
        name = str(username or "")
        if not name:
            return 0
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=max(1, int(window_s)))
        ).isoformat(timespec="seconds")
        with Session(self.engine) as session:
            last_ok = session.scalar(
                select(func.max(ControlAuthEvent.ts)).where(
                    ControlAuthEvent.tenant == scope,
                    ControlAuthEvent.username == name,
                    ControlAuthEvent.event == "login_ok",
                )
            )
            query = select(func.count()).select_from(ControlAuthEvent).where(
                ControlAuthEvent.tenant == scope,
                ControlAuthEvent.username == name,
                ControlAuthEvent.event == "login_failed",
                ControlAuthEvent.ts >= cutoff,
            )
            if last_ok:
                query = query.where(ControlAuthEvent.ts > str(last_ok))
            return int(session.scalar(query) or 0)


class PasswordResetStore:
    """一次性密码重置 token；只存哈希，消费后标记 used。"""

    def __init__(self, engine: Any, *, ttl_s: int = 1800) -> None:
        self.engine = engine
        self.ttl_s = max(60, int(ttl_s))

    def create(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with Session(self.engine) as session, session.begin():
            # 同一用户只保留一个未使用 token，避免旧邮件一直有效。
            session.execute(
                delete(ControlPasswordReset).where(
                    ControlPasswordReset.user_id == str(user_id),
                    ControlPasswordReset.used_at == "",
                )
            )
            session.add(
                ControlPasswordReset(
                    token_hash=_token_hash(token),
                    user_id=str(user_id),
                    created_at=now,
                    expires_at=_future(self.ttl_s),
                    used_at="",
                )
            )
        return token

    def consume(
        self, token: str, new_password: str
    ) -> str | None:
        """校验并消费 token；成功返回 user_id，失败返回 None。"""
        text = str(token or "")
        if not text:
            return None
        now = _now()
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlPasswordReset, _token_hash(text))
            if (
                row is None
                or row.used_at
                or row.expires_at <= now
            ):
                return None
            user = session.get(ControlUser, row.user_id)
            if user is None or user.status != "active":
                return None
            user.password_hash = hash_password(new_password)
            user.updated_at = now
            row.used_at = now
            session.flush()
            return str(row.user_id)

    def purge_expired(self) -> int:
        now = _now()
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                delete(ControlPasswordReset).where(
                    ControlPasswordReset.expires_at <= now
                )
            )
            return int(result.rowcount or 0)


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class SessionStore:
    """登录会话；token 明文只返回给客户端，服务端只存哈希。"""

    def __init__(
        self,
        engine: Any,
        *,
        ttl_s: int = 12 * 3600,
        idle_ttl_s: int = 0,
        cookie_name: str = "ca_session",
        csrf_cookie: str = "ca_csrf",
    ) -> None:
        self.engine = engine
        self.ttl_s = max(300, int(ttl_s))
        self.idle_ttl_s = max(0, int(idle_ttl_s))
        self.cookie_name = cookie_name
        self.csrf_cookie = csrf_cookie

    def _deadline(self, created_at: str, now: datetime) -> str:
        """空闲截止时间，但不能超过 created_at + 绝对 TTL。"""
        try:
            created = datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            created = now
        absolute = created + timedelta(seconds=self.ttl_s)
        if self.idle_ttl_s <= 0:
            return absolute.isoformat(timespec="seconds")
        target = now + timedelta(seconds=self.idle_ttl_s)
        return min(target, absolute).isoformat(timespec="seconds")

    def create(self, user_id: str) -> dict[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now_dt = datetime.now(UTC)
        created = now_dt.isoformat(timespec="seconds")
        expires = self._deadline(created, now_dt)
        with Session(self.engine) as session, session.begin():
            session.add(
                ControlAuthSession(
                    token_hash=_token_hash(token),
                    user_id=str(user_id),
                    csrf_secret=csrf,
                    expires_at=expires,
                    revoked_at="",
                    created_at=created,
                )
            )
        return {"token": token, "csrf": csrf, "expires_at": expires}

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
            self._touch(session_row)
            return _public(user), session_row.csrf_secret

    def _touch(self, row: ControlAuthSession) -> None:
        """有活动时顺延空闲截止；最多每 60 秒写一次库。"""
        if self.idle_ttl_s <= 0 or not row.created_at:
            return
        now_dt = datetime.now(UTC)
        deadline = self._deadline(row.created_at, now_dt)
        try:
            current = datetime.fromisoformat(row.expires_at)
            extended = datetime.fromisoformat(deadline)
        except (TypeError, ValueError):
            current = None
        if current is not None and (extended - current).total_seconds() < 60:
            return
        with Session(self.engine) as session, session.begin():
            stored = session.get(ControlAuthSession, row.token_hash)
            if stored is not None and not stored.revoked_at:
                stored.expires_at = deadline

    def list_for_user(
        self, user_id: str, *, current_token: str = ""
    ) -> list[dict[str, Any]]:
        """列出该账号仍有效的会话；不返回 token/哈希。"""
        now = _now()
        current = _token_hash(current_token) if current_token else ""
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlAuthSession)
                .where(
                    ControlAuthSession.user_id == str(user_id),
                    ControlAuthSession.revoked_at == "",
                    ControlAuthSession.expires_at > now,
                )
                .order_by(ControlAuthSession.created_at.desc())
            ).all()
            return [
                {
                    "created_at": row.created_at,
                    "expires_at": row.expires_at,
                    "current": bool(current)
                    and row.token_hash == current,
                }
                for row in rows
            ]

    def revoke_others(
        self, user_id: str, *, keep_token: str = ""
    ) -> int:
        """登出该账号除当前会话外的所有会话；返回吊销数量。"""
        keep = _token_hash(keep_token) if keep_token else ""
        now = _now()
        with Session(self.engine) as session, session.begin():
            rows = session.scalars(
                select(ControlAuthSession).where(
                    ControlAuthSession.user_id == str(user_id),
                    ControlAuthSession.revoked_at == "",
                    ControlAuthSession.token_hash != keep,
                )
            ).all()
            for row in rows:
                row.revoked_at = now
            return len(rows)

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
