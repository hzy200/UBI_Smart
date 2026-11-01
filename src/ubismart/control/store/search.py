"""跨会话全文检索与索引维护。

从 `control/store.py` 拆出的聚合根 mixin；方法体未改动。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from ubismart.control.schema import (
    ControlAuditIndex,
    ControlSession,
    ControlSessionSearch,
)

from .base import _LOG


class SearchMixin:
    """`ControlStore` 的跨会话全文检索与索引维护。"""

    @staticmethod
    def _search_row(
        thread_id: str, title: str, body: str, query: str
    ) -> dict[str, Any]:
        text_body = str(body or "")
        needle = str(query or "")
        index = text_body.lower().find(needle.lower()) if needle else -1
        if index < 0:
            snippet = text_body[:160]
        else:
            start = max(0, index - 40)
            end = min(len(text_body), index + len(needle) + 80)
            snippet = (
                ("…" if start else "")
                + text_body[start:end]
                + ("…" if end < len(text_body) else "")
            )
        return {
            "thread_id": thread_id,
            "title": title or "",
            "snippet": snippet,
        }

    def index_session_text(
        self,
        tenant: str,
        thread_id: str,
        *,
        title: str = "",
        body: str = "",
    ) -> None:
        """写入/更新跨会话搜索索引；SQLite 同步维护 FTS5。"""
        scope = str(tenant or "")
        thread = str(thread_id or "")
        if not scope or not thread:
            return
        clean_title = str(title or "")[:160]
        clean_body = str(body or "")[:20000]
        now = datetime.now(UTC).isoformat(timespec="seconds")
        final_title = clean_title
        final_body = clean_body
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlSessionSearch, (scope, thread))
            if row is None:
                row = ControlSessionSearch(
                    tenant=scope,
                    thread_id=thread,
                    title=clean_title,
                    body=clean_body,
                    updated_at=now,
                )
                session.add(row)
            else:
                if clean_title:
                    row.title = clean_title
                if clean_body:
                    row.body = clean_body
                row.updated_at = now
                # 只改标题时正文保持不变；FTS 也必须同步最终值。
                final_title = row.title
                final_body = row.body
        if self.engine.dialect.name != "sqlite":
            return
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM control_session_fts "
                        "WHERE tenant = :tenant AND thread_id = :thread"
                    ),
                    {"tenant": scope, "thread": thread},
                )
                connection.execute(
                    text(
                        "INSERT INTO control_session_fts"
                        "(tenant, thread_id, title, body) "
                        "VALUES (:tenant, :thread, :title, :body)"
                    ),
                    {
                        "tenant": scope,
                        "thread": thread,
                        "title": final_title,
                        "body": final_body,
                    },
                )
        except Exception:  # noqa: BLE001 - FTS 不可用时仍可 LIKE 搜索
            _LOG.warning("FTS 索引写入失败，回退 LIKE 搜索", exc_info=True)

    def _fts_rows(
        self, scope: str, needle: str, size: int
    ) -> list[Any]:
        if self.engine.dialect.name != "sqlite" or len(needle) < 3:
            return []
        fts_query = '"' + needle.replace('"', '""') + '"'
        try:
            with self.engine.connect() as connection:
                return connection.execute(
                    text(
                        "SELECT thread_id, title, body "
                        "FROM control_session_fts "
                        "WHERE tenant = :tenant "
                        "AND control_session_fts MATCH :query "
                        "ORDER BY rank LIMIT :limit"
                    ),
                    {
                        "tenant": scope,
                        "query": fts_query,
                        "limit": size,
                    },
                ).all()
        except Exception:  # noqa: BLE001 - FTS 缺失/语法不支持时回退
            return []

    def _like_rows(self, scope: str, needle: str, size: int) -> list[Any]:
        pattern = f"%{needle}%"
        with Session(self.engine) as session:
            indexed = session.scalars(
                select(ControlSessionSearch)
                .where(ControlSessionSearch.tenant == scope)
                .where(
                    or_(
                        ControlSessionSearch.title.like(pattern),
                        ControlSessionSearch.body.like(pattern),
                    )
                )
                .order_by(ControlSessionSearch.updated_at.desc())
                .limit(size)
            ).all()
        return [(row.thread_id, row.title, row.body) for row in indexed]

    def _add_transcript_hits(
        self,
        scope: str,
        needle: str,
        size: int,
        add: Any,
    ) -> None:
        # 1) transcript 正文（FTS5/LIKE）
        rows = self._fts_rows(scope, needle, size)
        if not rows:
            rows = self._like_rows(scope, needle, size)
        for row in rows:
            thread_id, title, body = row[0], row[1], row[2]
            item = self._search_row(
                str(thread_id), str(title), str(body), needle
            )
            add(
                item["thread_id"],
                item["title"],
                item["snippet"],
                "transcript",
            )

    def _add_session_hit(self, row: Any, needle: str, add: Any) -> None:
        match_text = (
            row.last_message
            if needle.lower() in str(row.last_message).lower()
            else row.title
        )
        snippet = self._search_row(
            row.thread_id, row.title, match_text, needle
        )["snippet"]
        add(row.thread_id, row.title or row.thread_id, snippet, "title")

    def _add_audit_hits(
        self,
        session: Any,
        scope: str,
        pattern: str,
        size: int,
        add: Any,
        merged: dict[str, dict[str, Any]],
    ) -> None:
        # 3) 审计索引：kind/tool/level/decision/thread_id
        audit_rows = session.scalars(
            select(ControlAuditIndex)
            .where(
                ControlAuditIndex.tenant == scope,
                or_(
                    ControlAuditIndex.thread_id.like(pattern),
                    ControlAuditIndex.kind.like(pattern),
                    ControlAuditIndex.tool.like(pattern),
                    ControlAuditIndex.level.like(pattern),
                    ControlAuditIndex.decision.like(pattern),
                ),
            )
            .order_by(ControlAuditIndex.ts.desc())
            .limit(size * 4)
        ).all()
        missing = [
            row.thread_id
            for row in audit_rows
            if row.thread_id and row.thread_id not in merged
        ]
        titles: dict[str, str] = {}
        if missing:
            for session_row in session.scalars(
                select(ControlSession).where(
                    ControlSession.tenant == scope,
                    ControlSession.thread_id.in_(sorted(set(missing))),
                )
            ).all():
                titles[session_row.thread_id] = session_row.title or ""
        for row in audit_rows:
            snippet = "审计：" + " ".join(
                part
                for part in (
                    row.kind,
                    row.tool,
                    row.level,
                    row.decision,
                )
                if part
            )
            add(
                row.thread_id,
                titles.get(row.thread_id, ""),
                snippet,
                "audit",
            )

    def _add_catalog_hits(
        self,
        scope: str,
        needle: str,
        size: int,
        add: Any,
        merged: dict[str, dict[str, Any]],
    ) -> None:
        pattern = f"%{needle}%"
        # 2) 会话目录：标题 + 最后消息（覆盖没有 transcript 的旧会话）
        with Session(self.engine) as session:
            sessions = session.scalars(
                select(ControlSession)
                .where(
                    ControlSession.tenant == scope,
                    ControlSession.deleted.is_(False),
                    or_(
                        ControlSession.title.like(pattern),
                        ControlSession.last_message.like(pattern),
                    ),
                )
                .order_by(ControlSession.updated_at.desc())
                .limit(size)
            ).all()
            for row in sessions:
                self._add_session_hit(row, needle, add)
            self._add_audit_hits(
                session, scope, pattern, size, add, merged
            )

    def search_sessions(
        self, tenant: str, query: str, *, limit: int = 20
    ) -> dict[str, Any]:
        scope = str(tenant or "")
        needle = str(query or "").strip()
        if not scope or not needle:
            return {"results": [], "enabled": True}
        size = max(1, min(100, int(limit)))
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []

        def add(
            thread_id: str,
            title: str,
            snippet: str,
            match: str,
        ) -> None:
            thread = str(thread_id or "")
            if not thread or thread in merged:
                return
            merged[thread] = {
                "thread_id": thread,
                "title": str(title or ""),
                "snippet": str(snippet or ""),
                "match": match,
            }
            order.append(thread)

        self._add_transcript_hits(scope, needle, size, add)
        self._add_catalog_hits(scope, needle, size, add, merged)
        results = [merged[thread] for thread in order][:size]
        return {"results": results, "enabled": True}

    def rebuild_session_search(
        self, tenants: list[str] | None = None
    ) -> int:
        """从会话目录重建标题/摘要索引。

        transcript 正文无法从控制面反推；已有的正文会保留，空正文用
        `control_sessions.last_message` 兜底。完整正文在下次保存轨迹时补齐。
        """
        scope = [str(item) for item in (tenants or []) if str(item)]
        query = select(ControlSession)
        if scope:
            query = query.where(ControlSession.tenant.in_(scope))
        search_query = select(ControlSessionSearch)
        if scope:
            search_query = search_query.where(
                ControlSessionSearch.tenant.in_(scope)
            )
        with Session(self.engine) as session:
            sessions = session.scalars(query).all()
            existing = {
                (row.tenant, row.thread_id): row.body
                for row in session.scalars(search_query).all()
            }
        for item in sessions:
            body = existing.get((item.tenant, item.thread_id), "")
            self.index_session_text(
                item.tenant,
                item.thread_id,
                title=item.title,
                body=body or item.last_message,
            )
        return len(sessions)

    def count_sessions(self, tenant: str) -> int:
        with Session(self.engine) as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(ControlSession)
                    .where(ControlSession.tenant == str(tenant))
                )
                or 0
            )
