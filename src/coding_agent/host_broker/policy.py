"""客户端授权与 ZX 判定。

判定顺序固定：ZX → grant 存在/撤销/过期 → scope → verb → 敏感根。
ZX 永远优先于 grant；敏感根必须逐次确认，不能靠长 TTL 宽 scope 复用。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from coding_agent.host_broker.protocol import (
    DENIED_SCOPE,
    DENIED_VERB,
    DENIED_ZX,
    GRANT_EXPIRED,
    GRANT_REQUIRED,
    GRANT_REVOKED,
    SENSITIVE_ROOT,
)

VERBS: frozenset[str] = frozenset(
    {"read", "write", "exec", "install", "network"}
)
MAX_TTL_S = 8 * 60 * 60
DEFAULT_TTL_S = 30 * 60


def _home() -> Path:
    return Path.home()


def _onedrive_roots() -> list[str]:
    roots: list[str] = []
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        value = os.environ.get(name)
        if value:
            roots.append(value)
    return roots


def default_deny_roots() -> tuple[str, ...]:
    home = _home()
    if os.name == "nt":
        roots = [
            r"C:\Windows",
            r"C:\Program Files",
            r"C:\Program Files (x86)",
            r"C:\ProgramData",
            str(home / ".ssh"),
            str(home / ".aws"),
            str(home / ".kube"),
            str(home / "AppData" / "Roaming" / "Microsoft" / "Credentials"),
            str(home / "AppData" / "Local" / "Microsoft" / "Credentials"),
            str(home / "AppData" / "Roaming" / "Microsoft" / "Protect"),
            str(home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"),
            str(home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"),
        ]
        return tuple(roots)
    return (
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/var",
        "/proc",
        "/sys",
        "/dev",
        "/root",
        str(home / ".ssh"),
        str(home / ".aws"),
        str(home / ".kube"),
        str(home / ".config" / "gcloud"),
        str(home / ".config" / "google-chrome"),
        str(home / ".mozilla" / "firefox"),
    )


def default_sensitive_roots() -> tuple[str, ...]:
    home = _home()
    roots = [
        str(home / "Documents"),
        str(home / "Desktop"),
        str(home / "Downloads"),
    ]
    roots.extend(_onedrive_roots())
    if os.name != "nt":
        roots.extend(
            [
                str(home / ".config" / "google-chrome"),
                str(home / ".mozilla" / "firefox"),
            ]
        )
    return tuple(roots)


def _norm(path: str) -> str:
    text = os.path.expandvars(os.path.expanduser(str(path).strip()))
    return os.path.normcase(os.path.normpath(os.path.abspath(text)))


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([_norm(path), _norm(root)]) == _norm(root)
    except ValueError:
        return False


def _split_roots(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        items = [item.strip() for item in values.split(",")]
    else:
        items = [str(item).strip() for item in values]
    out: list[str] = []
    for item in items:
        if not item:
            continue
        expanded = os.path.expandvars(os.path.expanduser(item))
        if not os.path.isabs(expanded):
            raise ValueError(f"授权/拒绝根必须是绝对路径：{item!r}")
        out.append(os.path.normpath(expanded))
    return out


def _dedupe_roots(roots: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for root in roots:
        key = os.path.normcase(os.path.normpath(root))
        if key not in seen:
            seen.add(key)
            out.append(os.path.normpath(root))
    return tuple(out)


def normalize_scope(path: str) -> str:
    text = os.path.expandvars(os.path.expanduser(str(path).strip()))
    if not text:
        raise ValueError("scope 不能为空")
    if not os.path.isabs(text):
        raise ValueError(f"scope 必须是绝对路径：{path!r}")
    normalized = os.path.normpath(text)
    if os.path.dirname(normalized) == normalized:
        raise ValueError("不允许把文件系统根作为 scope")
    if os.path.normcase(normalized) == os.path.normcase(
        os.path.normpath(str(_home()))
    ):
        raise ValueError("不允许把用户 home 根作为 scope")
    return normalized


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    grant_id: str
    scope: tuple[str, ...]
    verbs: frozenset[str]
    expires_at: datetime
    issued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    session_id: str = ""
    thread_id: str = ""
    reason: str = ""
    sensitive: bool = False
    revocable: bool = True
    revoked: bool = False

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if self.expires_at.tzinfo is None:
            return now.replace(tzinfo=None) >= self.expires_at
        return now >= self.expires_at

    def allows_path(self, path: str) -> bool:
        return any(_within(path, scope) for scope in self.scope)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    code: str = ""
    reason: str = ""
    verb: str = ""
    sensitive: bool = False
    grant_id: str = ""


def _deny(code: str, reason: str, *, verb: str = "", sensitive: bool = False,
          grant_id: str = "") -> PolicyDecision:
    return PolicyDecision(
        allowed=False,
        code=code,
        reason=reason,
        verb=verb,
        sensitive=sensitive,
        grant_id=grant_id,
    )


def _allow(*, verb: str, sensitive: bool, grant_id: str) -> PolicyDecision:
    return PolicyDecision(
        allowed=True,
        verb=verb,
        sensitive=sensitive,
        grant_id=grant_id,
    )


class GrantStore:
    """客户端本地 grant 存储；服务端不持有这里的 scope/TTL。"""

    def __init__(self) -> None:
        self._grants: dict[str, CapabilityGrant] = {}

    def issue(
        self,
        *,
        scope: Iterable[str],
        verbs: Iterable[str],
        ttl_s: int = DEFAULT_TTL_S,
        reason: str = "",
        session_id: str = "",
        thread_id: str = "",
        sensitive: bool = False,
        now: datetime | None = None,
    ) -> CapabilityGrant:
        scope_items = (scope,) if isinstance(scope, str) else tuple(scope)
        verb_items = (verbs,) if isinstance(verbs, str) else tuple(verbs)
        scopes = tuple(normalize_scope(item) for item in scope_items)
        if not scopes:
            raise ValueError("grant 至少需要一个 scope")
        normalized_verbs = frozenset(
            str(item).strip().lower() for item in verb_items if str(item).strip()
        )
        if not normalized_verbs:
            raise ValueError("grant 至少需要一个 verb")
        unknown = normalized_verbs - VERBS
        if unknown:
            raise ValueError(f"未知 verb：{sorted(unknown)}")
        ttl = int(ttl_s)
        if ttl <= 0 or ttl > MAX_TTL_S:
            raise ValueError(f"ttl_s 必须在 1..{MAX_TTL_S} 之间")
        issued = now or datetime.now(UTC)
        grant = CapabilityGrant(
            grant_id=f"g_{uuid.uuid4().hex}",
            scope=scopes,
            verbs=normalized_verbs,
            issued_at=issued,
            expires_at=issued + timedelta(seconds=ttl),
            session_id=str(session_id or ""),
            thread_id=str(thread_id or ""),
            reason=str(reason or ""),
            sensitive=bool(sensitive),
        )
        self._grants[grant.grant_id] = grant
        return grant

    def get(self, grant_id: str) -> CapabilityGrant | None:
        return self._grants.get(str(grant_id or ""))

    def revoke(self, grant_id: str) -> bool:
        grant = self._grants.get(str(grant_id or ""))
        if grant is None:
            return False
        self._grants[grant.grant_id] = replace(grant, revoked=True)
        return True

    def active(self, now: datetime | None = None) -> list[CapabilityGrant]:
        return [
            grant
            for grant in self._grants.values()
            if not grant.revoked and not grant.is_expired(now)
        ]

    def clear(self) -> None:
        self._grants.clear()


class HostPolicy:
    def __init__(
        self,
        *,
        deny_roots: Iterable[str] | str | None = None,
        sensitive_roots: Iterable[str] | str | None = None,
        root_policy: str = "local_ntfs_only",
    ) -> None:
        deny = default_deny_roots() if deny_roots is None else ()
        sensitive = (
            default_sensitive_roots() if sensitive_roots is None else ()
        )
        self.deny_roots = _dedupe_roots(
            [*deny, *_split_roots(deny_roots)]
        )
        self.sensitive_roots = _dedupe_roots(
            [*sensitive, *_split_roots(sensitive_roots)]
        )
        self.root_policy = str(root_policy or "local_ntfs_only")

    @classmethod
    def from_settings(cls, settings: Any) -> HostPolicy:
        deny = [*default_deny_roots(), *_split_roots(settings.host_deny_roots)]
        sensitive = [
            *default_sensitive_roots(),
            *_split_roots(settings.host_sensitive_roots),
        ]
        return cls(
            deny_roots=deny,
            sensitive_roots=sensitive,
            root_policy=settings.host_root_policy,
        )

    def matching_deny_root(self, path: str) -> str:
        for root in self.deny_roots:
            if _within(path, root):
                return root
        return ""

    def matching_sensitive_root(self, path: str) -> str:
        for root in self.sensitive_roots:
            if _within(path, root):
                return root
        return ""

    def is_sensitive(self, path: str) -> bool:
        return bool(self.matching_sensitive_root(path))

    def check(
        self,
        path: str,
        verb: str,
        grant: CapabilityGrant | None,
        *,
        sensitive_ack: bool = False,
        now: datetime | None = None,
    ) -> PolicyDecision:
        verb = str(verb or "").strip().lower()
        if verb not in VERBS:
            return _deny(DENIED_VERB, f"未知动词：{verb!r}", verb=verb)
        deny_root = self.matching_deny_root(path)
        if deny_root:
            return _deny(
                DENIED_ZX,
                f"路径命中绝对禁止根：{deny_root}",
                verb=verb,
            )
        sensitive = self.is_sensitive(path)
        if grant is None:
            return _deny(
                GRANT_REQUIRED,
                "需要本地授权",
                verb=verb,
                sensitive=sensitive,
            )
        if grant.revoked:
            return _deny(
                GRANT_REVOKED,
                "授权已撤销",
                verb=verb,
                sensitive=sensitive,
                grant_id=grant.grant_id,
            )
        if grant.is_expired(now):
            return _deny(
                GRANT_EXPIRED,
                "授权已过期",
                verb=verb,
                sensitive=sensitive,
                grant_id=grant.grant_id,
            )
        if not grant.allows_path(path):
            return _deny(
                DENIED_SCOPE,
                "路径不在授权 scope 内",
                verb=verb,
                sensitive=sensitive,
                grant_id=grant.grant_id,
            )
        if verb not in grant.verbs:
            return _deny(
                DENIED_VERB,
                f"授权不含动词：{verb}",
                verb=verb,
                sensitive=sensitive,
                grant_id=grant.grant_id,
            )
        if sensitive and not (grant.sensitive or sensitive_ack):
            return _deny(
                SENSITIVE_ROOT,
                "敏感根需要逐次本地确认",
                verb=verb,
                sensitive=True,
                grant_id=grant.grant_id,
            )
        return _allow(verb=verb, sensitive=sensitive, grant_id=grant.grant_id)
