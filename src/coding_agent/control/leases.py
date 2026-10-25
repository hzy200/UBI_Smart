"""会话级运行租约（RFC-0001，M4）。

A2 在转发 run/resume 前抢占，A2 崩溃后由 TTL 自动失效。
A3 不连接 MySQL，仍保留自己的进程内活跃判断；两层是互补关系。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from coding_agent.control.schema import RunLease


class LeaseHeld(RuntimeError):
    """会话已被其他租约持有。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


class RunLeaseStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def acquire(
        self,
        session_key: str,
        owner: str,
        *,
        ttl_s: int = 120,
        lease_id: str = "",
    ) -> str:
        key = str(session_key or "").strip()
        if not key:
            raise ValueError("session_key 不能为空")
        lease = str(lease_id or uuid.uuid4())
        now = _now()
        expires = _fmt(now + timedelta(seconds=max(1, int(ttl_s))))
        with Session(self.engine) as session, session.begin():
            row = session.get(
                RunLease, key, with_for_update=True
            )
            if (
                row is not None
                and row.expires_at > _fmt(now)
                and row.lease_id != lease
            ):
                raise LeaseHeld(
                    f"会话 {key} 已被租约 {row.lease_id} 持有，"
                    f"到期 {row.expires_at}"
                )
            if row is None:
                session.add(
                    RunLease(
                        session_key=key,
                        lease_id=lease,
                        owner=str(owner or ""),
                        expires_at=expires,
                        created_at=_fmt(now),
                    )
                )
            else:
                row.lease_id = lease
                row.owner = str(owner or "")
                row.expires_at = expires
        return lease

    def renew(self, session_key: str, lease_id: str, *, ttl_s: int = 120) -> bool:
        now = _now()
        statement = (
            update(RunLease)
            .where(
                RunLease.session_key == str(session_key),
                RunLease.lease_id == str(lease_id),
            )
            .values(
                expires_at=_fmt(
                    now + timedelta(seconds=max(1, int(ttl_s)))
                )
            )
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            return bool(result.rowcount)

    def release(self, session_key: str, lease_id: str) -> bool:
        statement = delete(RunLease).where(
            RunLease.session_key == str(session_key),
            RunLease.lease_id == str(lease_id),
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            return bool(result.rowcount)

    def force_release(self, session_key: str) -> bool:
        statement = delete(RunLease).where(
            RunLease.session_key == str(session_key)
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            return bool(result.rowcount)

    def active(self, session_key: str) -> dict | None:
        with Session(self.engine) as session:
            row = session.get(RunLease, str(session_key))
            if row is None:
                return None
            if row.expires_at <= _fmt(_now()):
                return None
            return {
                "session_key": row.session_key,
                "lease_id": row.lease_id,
                "owner": row.owner,
                "expires_at": row.expires_at,
                "created_at": row.created_at,
            }

    def purge_expired(self) -> int:
        statement = delete(RunLease).where(
            RunLease.expires_at <= _fmt(_now())
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            return int(result.rowcount or 0)

    def list_active(self, owner: str = "") -> list[dict]:
        with Session(self.engine) as session:
            statement = select(RunLease).where(
                RunLease.expires_at > _fmt(_now())
            )
            if owner:
                statement = statement.where(RunLease.owner == owner)
            return [
                {
                    "session_key": row.session_key,
                    "lease_id": row.lease_id,
                    "owner": row.owner,
                    "expires_at": row.expires_at,
                }
                for row in session.scalars(statement).all()
            ]
