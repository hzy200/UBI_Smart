"""个人访问令牌（PAT）。

只保存 token 的 sha256；明文只在创建响应里出现一次。scope 只允许
`read` / `run` / `audit`，具体路由映射在 `web/auth.py`。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from coding_agent.control.schema import ControlApiToken

SCOPES = ("read", "run", "audit")
_PREFIX = "ca_pat_"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def normalize_scopes(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = list(raw or [])
    values = {
        str(item).strip().lower()
        for item in items
        if str(item).strip()
    }
    unknown = values - set(SCOPES)
    if unknown:
        raise ValueError(f"未知 scope：{','.join(sorted(unknown))}")
    return tuple(scope for scope in SCOPES if scope in values)


def _public(row: ControlApiToken) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "scopes": [
            item for item in str(row.scopes or "").split(",") if item
        ],
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
    }


class ApiTokenStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create(
        self,
        *,
        username: str,
        user_id: str = "",
        name: str,
        scopes: Any,
        expires_in_days: int = 0,
    ) -> tuple[dict[str, Any], str]:
        owner = str(username or "")
        if not owner:
            raise ValueError("PAT 必须绑定用户")
        selected = normalize_scopes(scopes)
        if not selected:
            raise ValueError("至少选择一个 scope")
        token = _PREFIX + secrets.token_urlsafe(32)
        now = _now()
        days = max(0, int(expires_in_days or 0))
        expires = (
            (datetime.now(UTC) + timedelta(days=days))
            .isoformat(timespec="seconds")
            if days
            else ""
        )
        row = ControlApiToken(
            id=secrets.token_hex(16),
            tenant=owner,
            user_id=str(user_id or ""),
            name=str(name or "").strip()[:64] or "PAT",
            token_hash=_digest(token),
            prefix=f"{token[:12]}…{token[-4:]}",
            scopes=",".join(selected),
            created_at=now,
            last_used_at="",
            expires_at=expires,
            revoked_at="",
        )
        with Session(self.engine) as session, session.begin():
            session.add(row)
            session.flush()
            return _public(row), token

    def list(self, username: str) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlApiToken)
                .where(ControlApiToken.tenant == str(username or ""))
                .order_by(ControlApiToken.created_at.desc())
            ).all()
            return [_public(row) for row in rows]

    def revoke(self, username: str, token_id: str) -> bool:
        now = _now()
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(ControlApiToken)
                .where(
                    ControlApiToken.id == str(token_id),
                    ControlApiToken.tenant == str(username or ""),
                    ControlApiToken.revoked_at == "",
                )
                .values(revoked_at=now)
            )
            return bool(result.rowcount)

    def resolve(self, token: str) -> dict[str, Any] | None:
        digest = _digest(token)
        if not digest:
            return None
        now = _now()
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(ControlApiToken).where(
                    ControlApiToken.token_hash == digest
                )
            )
            if row is None or row.revoked_at:
                return None
            if row.expires_at and row.expires_at <= now:
                return None
            # 高频请求不每次写库：距上次记录超过 60 秒才更新。
            stale = (
                not row.last_used_at
                or row.last_used_at
                < (
                    datetime.now(UTC) - timedelta(seconds=60)
                ).isoformat(timespec="seconds")
            )
            if stale:
                row.last_used_at = now
            return {
                "username": row.tenant,
                "user_id": row.user_id,
                "scopes": tuple(
                    item
                    for item in str(row.scopes or "").split(",")
                    if item
                ),
            }
