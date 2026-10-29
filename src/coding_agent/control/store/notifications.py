"""站内通知与 Web Push 订阅。

从 `control/store.py` 拆出的聚合根 mixin；方法体未改动。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from coding_agent.control.schema import (
    ControlNotification,
    ControlPushSubscription,
)


class NotificationsMixin:
    """`ControlStore` 的站内通知与 Web Push 订阅。"""

    @staticmethod
    def _notification_row(row: ControlNotification) -> dict[str, Any]:
        return {
            "id": row.id,
            "kind": row.kind,
            "title": row.title,
            "body": row.body,
            "thread_id": row.thread_id,
            "path": row.path,
            "read": bool(row.read),
            "created_at": row.created_at,
        }

    def add_notification(
        self,
        tenant: str,
        *,
        kind: str,
        title: str,
        body: str = "",
        thread_id: str = "",
        path: str = "",
        dedupe_key: str = "",
        created_at: str = "",
    ) -> dict[str, Any] | None:
        """写入一条站内通知；同一 dedupe_key 只保留第一次。"""
        scope = str(tenant or "")
        title = str(title or "").strip()[:160]
        if not scope or not title:
            return None
        dedupe = str(dedupe_key or "").strip()[:160] or None
        now = created_at or datetime.now(UTC).isoformat(timespec="seconds")
        with Session(self.engine) as session, session.begin():
            if dedupe:
                existing = session.scalar(
                    select(ControlNotification).where(
                        ControlNotification.tenant == scope,
                        ControlNotification.dedupe_key == dedupe,
                    )
                )
                if existing is not None:
                    return self._notification_row(existing)
            row = ControlNotification(
                id=uuid4().hex,
                tenant=scope,
                dedupe_key=dedupe,
                kind=str(kind or "info")[:24],
                title=title,
                body=str(body or "")[:500],
                thread_id=str(thread_id or "")[:64],
                path=str(path or "")[:512],
                read=False,
                created_at=now,
            )
            session.add(row)
            session.flush()
            stale = session.scalars(
                select(ControlNotification.id)
                .where(ControlNotification.tenant == scope)
                .order_by(
                    ControlNotification.created_at.desc(),
                    ControlNotification.id.desc(),
                )
                .offset(200)
            ).all()
            if stale:
                session.execute(
                    delete(ControlNotification).where(
                        ControlNotification.id.in_(list(stale))
                    )
                )
            return self._notification_row(row)

    def list_notifications(
        self, tenant: str, *, limit: int = 100
    ) -> dict[str, Any]:
        scope = str(tenant or "")
        if not scope:
            return {"notifications": [], "unread": 0, "enabled": True}
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlNotification)
                .where(ControlNotification.tenant == scope)
                .order_by(
                    ControlNotification.created_at.desc(),
                    ControlNotification.id.desc(),
                )
                .limit(max(1, min(200, int(limit))))
            ).all()
            unread = int(
                session.scalar(
                    select(func.count())
                    .select_from(ControlNotification)
                    .where(
                        ControlNotification.tenant == scope,
                        ControlNotification.read.is_(False),
                    )
                )
                or 0
            )
        return {
            "notifications": [self._notification_row(row) for row in rows],
            "unread": unread,
            "enabled": True,
        }

    def mark_notifications_read(
        self,
        tenant: str,
        *,
        ids: list[str] | None = None,
        all_: bool = False,
    ) -> int:
        scope = str(tenant or "")
        if not scope:
            return 0
        targets = [str(item) for item in (ids or []) if str(item)][:500]
        if not all_ and not targets:
            return 0
        with Session(self.engine) as session, session.begin():
            query = update(ControlNotification).where(
                ControlNotification.tenant == scope,
                ControlNotification.read.is_(False),
            )
            if not all_:
                query = query.where(ControlNotification.id.in_(targets))
            return int(
                session.execute(query.values(read=True)).rowcount or 0
            )

    def clear_notifications(self, tenant: str) -> int:
        scope = str(tenant or "")
        if not scope:
            return 0
        with Session(self.engine) as session, session.begin():
            return int(
                session.execute(
                    delete(ControlNotification).where(
                        ControlNotification.tenant == scope
                    )
                ).rowcount
                or 0
            )

    @staticmethod
    def _push_row(row: ControlPushSubscription) -> dict[str, Any]:
        return {
            "id": row.id,
            "tenant": row.tenant,
            "endpoint": row.endpoint,
            "p256dh": row.p256dh,
            "auth": row.auth,
            "user_agent": row.user_agent,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def add_push_subscription(
        self,
        tenant: str,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str = "",
    ) -> dict[str, Any] | None:
        scope = str(tenant or "")
        target = str(endpoint or "").strip()
        if not scope or not target:
            return None
        digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with Session(self.engine) as session, session.begin():
            row = session.scalar(
                select(ControlPushSubscription).where(
                    ControlPushSubscription.tenant == scope,
                    ControlPushSubscription.endpoint_hash == digest,
                )
            )
            if row is not None:
                row.p256dh = str(p256dh or "")[:255]
                row.auth = str(auth or "")[:128]
                row.user_agent = str(user_agent or "")[:255]
                row.updated_at = now
                return self._push_row(row)
            row = ControlPushSubscription(
                id=uuid4().hex,
                tenant=scope,
                endpoint_hash=digest,
                endpoint=target,
                p256dh=str(p256dh or "")[:255],
                auth=str(auth or "")[:128],
                user_agent=str(user_agent or "")[:255],
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            return self._push_row(row)

    def list_push_subscriptions(
        self, tenant: str
    ) -> list[dict[str, Any]]:
        scope = str(tenant or "")
        if not scope:
            return []
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlPushSubscription)
                .where(ControlPushSubscription.tenant == scope)
                .order_by(ControlPushSubscription.updated_at.desc())
            ).all()
            return [self._push_row(row) for row in rows]

    def remove_push_subscription(
        self,
        tenant: str,
        *,
        endpoint: str = "",
        endpoint_hash: str = "",
    ) -> bool:
        scope = str(tenant or "")
        target = str(endpoint or "").strip()
        digest = str(endpoint_hash or "").strip()
        if not scope or (not target and not digest):
            return False
        if not digest and target:
            digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                delete(ControlPushSubscription).where(
                    ControlPushSubscription.tenant == scope,
                    ControlPushSubscription.endpoint_hash == digest,
                )
            )
            return bool(result.rowcount)
