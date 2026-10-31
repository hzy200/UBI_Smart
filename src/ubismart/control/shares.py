"""只读分享链接：已脱敏快照 + 哈希 token + 过期/撤销。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from coding_agent.control.schema import ControlShareLink

MAX_PAYLOAD_BYTES = 2_000_000
_PREFIX = "shr_"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _public(row: ControlShareLink) -> dict[str, Any]:
    return {
        "id": row.id,
        "thread_id": row.thread_id,
        "title": row.title,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "views": int(row.views or 0),
        "active": not row.revoked_at
        and (not row.expires_at or row.expires_at > _now()),
    }


class ShareStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create(
        self,
        tenant: str,
        *,
        thread_id: str,
        created_by: str,
        title: str,
        payload: dict[str, Any],
        expires_in_days: int = 7,
    ) -> tuple[dict[str, Any], str]:
        text = json.dumps(payload, ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("分享快照超过 2MB，请减少会话长度")
        token = _PREFIX + secrets.token_urlsafe(32)
        days = max(1, min(30, int(expires_in_days or 7)))
        now = datetime.now(UTC)
        row = ControlShareLink(
            id=secrets.token_hex(16),
            token_hash=_digest(token),
            tenant=str(tenant or ""),
            thread_id=str(thread_id or ""),
            created_by=str(created_by or ""),
            title=str(title or "")[:160],
            payload=text,
            created_at=now.isoformat(timespec="seconds"),
            expires_at=(now + timedelta(days=days)).isoformat(
                timespec="seconds"
            ),
            revoked_at="",
            views=0,
        )
        with Session(self.engine) as session, session.begin():
            session.add(row)
            session.flush()
            return _public(row), token

    def list(self, tenant: str, thread_id: str) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlShareLink)
                .where(
                    ControlShareLink.tenant == str(tenant or ""),
                    ControlShareLink.thread_id == str(thread_id or ""),
                )
                .order_by(ControlShareLink.created_at.desc())
            ).all()
            return [_public(row) for row in rows]

    def revoke(self, tenant: str, share_id: str) -> bool:
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(ControlShareLink)
                .where(
                    ControlShareLink.id == str(share_id),
                    ControlShareLink.tenant == str(tenant or ""),
                    ControlShareLink.revoked_at == "",
                )
                .values(revoked_at=_now())
            )
            return bool(result.rowcount)

    def resolve(self, token: str) -> dict[str, Any] | None:
        digest = _digest(token)
        if not digest:
            return None
        now = _now()
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(ControlShareLink).where(
                    ControlShareLink.token_hash == digest
                )
            )
            if row is None or row.revoked_at:
                return None
            if row.expires_at and row.expires_at <= now:
                return None
            row.views = int(row.views or 0) + 1
            try:
                payload = json.loads(row.payload or "{}")
            except ValueError:
                payload = {}
            return {
                "id": row.id,
                "title": row.title,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "views": int(row.views),
                "payload": payload if isinstance(payload, dict) else {},
            }
