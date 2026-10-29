"""A2 控制面的会话目录存储（Phase B1/B2）。

`AGENT_CONTROL_DB_URL` 为空时本模块不参与运行；设置后 Web 控制面先把
`gui-sessions.json` 里已有的会话导入一次，再以数据库为准。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    case,
    create_engine,
    delete,
    event,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coding_agent.control.schema import (
    CONTROL_SCHEMA_VERSION,
    Base,
    ControlAuditIndex,
    ControlMeta,
    ControlNotification,
    ControlPushSubscription,
    ControlQuotaReservation,
    ControlSession,
    ControlSessionSearch,
    ControlUsageCursor,
    ControlUsageEvent,
    ControlUserQuota,
    ControlUserUsageDaily,
    Workspace,
)

_LOG = logging.getLogger(__name__)

# 用量回写是低频动作；进程内串行化，避免同一 thread 的 cursor 读-改-写
# 在 SQLite（行锁被忽略）上互相覆盖。跨进程由 MySQL 的 cursor 行锁兜底。
_USAGE_LOCK = threading.Lock()


class QuotaExceeded(RuntimeError):
    """每日额度不足；携带给 API 层的稳定错误信息。"""

    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__("每日额度已用尽")
        self.details = details


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _future(seconds: int) -> str:
    return (
        datetime.now(UTC) + timedelta(seconds=max(1, int(seconds)))
    ).isoformat(timespec="seconds")


def _tags_from_text(value: str) -> list[str]:
    try:
        data = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()][:8]


def _tags_to_text(tags: Any) -> str:
    if not isinstance(tags, list):
        return "[]"
    clean: list[str] = []
    for item in tags:
        text = " ".join(str(item or "").split())[:24]
        if text and text not in clean:
            clean.append(text)
    return json.dumps(clean[:8], ensure_ascii=False)


class ControlStore:
    def __init__(
        self,
        url: str,
        *,
        auto_create: bool = True,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_recycle: int = 1800,
        pool_timeout: int = 30,
    ) -> None:
        text_url = str(url or "").strip()
        if not text_url:
            raise ValueError("control DB URL 为空")
        connect_args: dict[str, Any] = {}
        if text_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        kwargs: dict[str, Any] = {
            "future": True,
            "pool_pre_ping": True,
            "connect_args": connect_args,
        }
        if not text_url.startswith("sqlite"):
            # SQLite（开发/测试）不接受这些 QueuePool 参数。
            kwargs.update(
                pool_size=max(1, int(pool_size)),
                max_overflow=max(0, int(max_overflow)),
                pool_recycle=max(0, int(pool_recycle)),
                pool_timeout=max(1, int(pool_timeout)),
                pool_use_lifo=True,
            )
        self.engine: Engine = create_engine(text_url, **kwargs)
        if text_url.startswith("sqlite"):
            self._apply_sqlite_pragmas()
        if auto_create:
            self.ensure_schema()

    def _apply_sqlite_pragmas(self) -> None:
        """SQLite 的并发参数；缺了它们多 worker 会直接 `database is locked`。

        - `WAL`：读写不互相阻塞，否则 `control/backup.py` 的
          `PRAGMA wal_checkpoint(FULL)` 是空操作、备份点一致性无从谈起；
        - `busy_timeout`：写冲突时等锁而不是立刻报错；
        - `synchronous=NORMAL`：WAL 下的常规选择（崩溃可能丢最后几笔提交，
          控制面存的是会话目录与用量，可接受；审计另有 JSONL 哈希链兜底）。

        `:memory:` 与临时库会保持自己的 journal mode，`journal_mode=WAL`
        在那里是无害的空操作。
        """
        engine = self.engine

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_connection, _record):  # noqa: ANN001, ANN202
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    def ensure_schema(self) -> None:
        """首次建表；只创建缺失表，不做列迁移。"""
        Base.metadata.create_all(self.engine)
        self._ensure_search_fts()
        with Session(self.engine) as session, session.begin():
            if session.get(ControlMeta, "schema_version") is None:
                session.add(
                    ControlMeta(
                        key="schema_version", value=CONTROL_SCHEMA_VERSION
                    )
                )

    def _ensure_search_fts(self) -> None:
        """SQLite 上补建 FTS5 虚表；MySQL/旧 SQLite 回退 LIKE。"""
        if self.engine.dialect.name != "sqlite":
            return
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS "
                        "control_session_fts USING fts5("
                        "tenant UNINDEXED, thread_id UNINDEXED, title, body, "
                        "tokenize='trigram')"
                    )
                )
        except Exception:  # noqa: BLE001 - FTS 不可用不影响搜索功能
            _LOG.warning("FTS5 不可用，跨会话搜索回退 LIKE", exc_info=True)

    def load_sessions(
        self, tenant: str
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """返回（未删除会话，已删除 thread 集合）。"""
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlSession).where(
                    ControlSession.tenant == str(tenant)
                )
            ).all()
        active: list[dict[str, Any]] = []
        deleted: set[str] = set()
        for row in rows:
            if row.deleted:
                deleted.add(row.thread_id)
                continue
            active.append(
                {
                    "thread_id": row.thread_id,
                    "workspace": row.workspace,
                    "workspace_id": row.workspace_id,
                    "workspace_kind": row.workspace_kind,
                    "a2l_id": row.a2l_id,
                    "root_handle": row.root_handle,
                    "access_mode": row.access_mode,
                    "model": row.model,
                    "provider": row.provider,
                    "uploaded_bytes": int(row.uploaded_bytes or 0),
                    "title": row.title,
                    "pinned": bool(row.pinned),
                    "archived": bool(row.archived),
                    "tags": _tags_from_text(row.tags),
                    "unread": bool(row.unread),
                    "last_message": row.last_message,
                }
            )
        return active, deleted

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        """读取工作区元数据；会话配置重建时用它恢复 host 绑定。"""
        key = str(workspace_id or "")
        if not key:
            return None
        with Session(self.engine) as session:
            row = session.get(Workspace, key)
            if row is None:
                return None
            return {
                "workspace_id": row.workspace_id,
                "display_name": row.display_name,
                "kind": row.kind,
                "a2l_id": row.a2l_id,
                "root_handle": row.root_handle,
                "root_path": row.root_path,
                "status": row.status,
            }

    def save_sessions(
        self,
        tenant: str,
        sessions: list[dict[str, Any]],
        deleted: set[str],
    ) -> None:
        """在一个事务里 upsert 会话并写入 tombstone。"""
        now = _now()
        with Session(self.engine) as session, session.begin():
            for data in sessions:
                thread_id = str(data.get("thread_id") or "")
                if not thread_id:
                    continue
                row = session.get(ControlSession, (str(tenant), thread_id))
                if row is None:
                    row = ControlSession(
                        tenant=str(tenant),
                        thread_id=thread_id,
                        created_at=now,
                    )
                    session.add(row)
                row.workspace = str(data.get("workspace") or "")
                row.workspace_id = str(
                    data.get("workspace_id") or ""
                )[:36]
                row.workspace_kind = str(
                    data.get("workspace_kind") or ""
                )[:16]
                row.a2l_id = str(data.get("a2l_id") or "")[:64]
                row.root_handle = str(
                    data.get("root_handle") or ""
                )[:128]
                row.access_mode = str(
                    data.get("access_mode") or "read_only"
                )
                row.model = str(data.get("model") or "")
                row.provider = str(data.get("provider") or "")
                row.uploaded_bytes = int(data.get("uploaded_bytes") or 0)
                row.title = str(data.get("title") or "")
                if "pinned" in data:
                    row.pinned = bool(data.get("pinned"))
                if "archived" in data:
                    row.archived = bool(data.get("archived"))
                if "tags" in data:
                    row.tags = _tags_to_text(data.get("tags"))
                if "unread" in data:
                    row.unread = bool(data.get("unread"))
                if "last_message" in data:
                    row.last_message = str(
                        data.get("last_message") or ""
                    )[:160]
                row.deleted = False
                row.updated_at = now
            for thread_id in deleted:
                thread = str(thread_id or "")
                if not thread:
                    continue
                row = session.get(ControlSession, (str(tenant), thread))
                if row is None:
                    row = ControlSession(
                        tenant=str(tenant),
                        thread_id=thread,
                        workspace="",
                        created_at=now,
                    )
                    session.add(row)
                row.deleted = True
                row.updated_at = now

    def index_audit(
        self,
        tenant: str,
        records: list[dict[str, Any]],
        *,
        usage_tenant: str = "",
        usage_user: str = "",
    ) -> int:
        """把 A3 拉回来的审计记录幂等写入查询索引。

        只查询本批记录的 hash，不拉取该租户的全部历史。此前每次调用都会
        把整张索引表的 hash 读进内存，成本随累计审计量线性增长；审计查询
        和每次运行结束都会走到这里，长跑服务上会越来越慢。
        """
        added = 0
        hashes = {
            str(record.get("hash") or "") for record in records
        } - {""}
        known: set[str] = set()
        with Session(self.engine) as session, session.begin():
            # SQLite 的绑定参数上限是 999；按 500 分块，MySQL 同样安全。
            ordered = sorted(hashes)
            for offset in range(0, len(ordered), 500):
                known.update(
                    session.scalars(
                        select(ControlAuditIndex.record_hash).where(
                            ControlAuditIndex.tenant == str(tenant),
                            ControlAuditIndex.record_hash.in_(
                                ordered[offset : offset + 500]
                            ),
                        )
                    )
                )
            for record in records:
                record_hash = str(record.get("hash") or "")
                if record_hash and record_hash in known:
                    continue
                session.add(
                    ControlAuditIndex(
                        tenant=str(tenant),
                        thread_id=str(record.get("thread_id") or ""),
                        ts=str(record.get("ts") or ""),
                        kind=str(record.get("kind") or ""),
                        tool=str(record.get("tool") or ""),
                        level=str(record.get("level") or ""),
                        decision=str(record.get("decision") or ""),
                        ok=record.get("ok"),
                        record_hash=record_hash,
                    )
                )
                if record_hash:
                    known.add(record_hash)
                added += 1
        if added and usage_tenant and usage_user:
            # 用量单独事务：失败不能回滚审计索引，下一次终态还会补。
            try:
                self.record_usage(
                    usage_tenant, usage_user, records
                )
            except Exception:  # noqa: BLE001 - 计量失败不阻断审计
                _LOG.warning("用量累加失败", exc_info=True)
        return added

    def list_audit(
        self,
        tenants: list[str],
        *,
        user: str = "",
        thread_id: str = "",
        kind: str = "",
        tool: str = "",
        ok: bool | None = None,
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按组织成员查询审计索引。

        索引里的 `tenant` 是用户名（与会话/用量作用域一致）；调用方必须先
        用组织成员列表收窄范围，不能把任意用户名传进来。
        """
        scope = sorted({str(item) for item in tenants if str(item)})
        if user:
            scope = [str(user)] if str(user) in scope else []
        if not scope:
            return {"records": [], "total": 0, "has_more": False}
        query = select(ControlAuditIndex).where(
            ControlAuditIndex.tenant.in_(scope)
        )
        if thread_id:
            query = query.where(ControlAuditIndex.thread_id == str(thread_id))
        if kind:
            query = query.where(ControlAuditIndex.kind == str(kind))
        if tool:
            query = query.where(ControlAuditIndex.tool == str(tool))
        if ok is not None:
            query = query.where(ControlAuditIndex.ok.is_(bool(ok)))
        if since:
            query = query.where(ControlAuditIndex.ts >= str(since))
        if until:
            query = query.where(ControlAuditIndex.ts <= str(until))
        with Session(self.engine) as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(query.subquery())
                )
                or 0
            )
            rows = session.scalars(
                query.order_by(
                    ControlAuditIndex.ts.desc(),
                    ControlAuditIndex.id.desc(),
                )
                .offset(max(0, int(offset)))
                .limit(max(1, int(limit)))
            ).all()
        records = [
            {
                "tenant": row.tenant,
                "thread_id": row.thread_id,
                "ts": row.ts,
                "kind": row.kind,
                "tool": row.tool,
                "level": row.level,
                "decision": row.decision,
                "ok": row.ok,
                "record_hash": row.record_hash,
            }
            for row in rows
        ]
        return {
            "records": records,
            "total": total,
            "has_more": int(offset) + len(records) < total,
        }

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

        # 1) transcript 正文（FTS5/LIKE）
        fts_rows: list[Any] = []
        if self.engine.dialect.name == "sqlite" and len(needle) >= 3:
            fts_query = '"' + needle.replace('"', '""') + '"'
            try:
                with self.engine.connect() as connection:
                    fts_rows = connection.execute(
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
                fts_rows = []
        if not fts_rows:
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
            fts_rows = [
                (row.thread_id, row.title, row.body) for row in indexed
            ]
        for row in fts_rows:
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
                match_text = (
                    row.last_message
                    if needle.lower() in str(row.last_message).lower()
                    else row.title
                )
                snippet = self._search_row(
                    row.thread_id, row.title, match_text, needle
                )["snippet"]
                add(
                    row.thread_id,
                    row.title or row.thread_id,
                    snippet,
                    "title",
                )

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
                    titles[session_row.thread_id] = (
                        session_row.title or ""
                    )
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

    def ping(self) -> bool:
        """轻量连通性检查；供健康端点与降级判断使用。"""
        try:
            with self.engine.connect() as session:
                session.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - 数据库故障是预期状态
            return False

    def close(self) -> None:
        self.engine.dispose()

    # ---------- 企业用量与配额 ----------

    def _daily_add(
        self,
        session: Session,
        *,
        tenant: str,
        user: str,
        day: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        worker_input_tokens: int = 0,
        worker_output_tokens: int = 0,
        cost_usd: float = 0.0,
        billed_usd: float = 0.0,
        runs: int = 0,
        reserved_tokens: int = 0,
        reserved_usd: float = 0.0,
    ) -> None:
        """原子累加日用量；行不存在时插入。

        SQLite 会静默忽略 `SELECT ... FOR UPDATE`，所以增量必须落在一条
        `UPDATE ... SET x = x + ?`（或方言 upsert）里，不能先读再写回。
        预留下调用 CASE 夹到 0，避免并发结算把计数减成负数。
        """
        table = ControlUserUsageDaily.__table__
        adds: dict[str, int | float] = {
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "worker_input_tokens": int(worker_input_tokens),
            "worker_output_tokens": int(worker_output_tokens),
            "cost_usd": float(cost_usd),
            "billed_usd": float(billed_usd),
            "runs": int(runs),
            "reserved_tokens": int(reserved_tokens),
            "reserved_usd": float(reserved_usd),
        }
        values: dict[str, Any] = {
            "tenant": tenant,
            "user": user,
            "day": day,
        }
        for key, delta in adds.items():
            if not delta:
                continue
            values[key] = (
                max(0, delta) if key.startswith("reserved") else delta
            )
        set_: dict[Any, Any] = {}
        for key, delta in adds.items():
            column = table.c[key]
            if key.startswith("reserved") and delta < 0:
                set_[column] = case(
                    (column + delta < 0, 0), else_=column + delta
                )
            else:
                set_[column] = column + delta

        dialect = self.engine.dialect.name
        if dialect == "sqlite":
            stmt = sqlite_insert(table).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    table.c.tenant,
                    table.c.user,
                    table.c.day,
                ],
                set_=set_,
            )
            session.execute(stmt)
            return
        if dialect == "mysql":
            stmt = mysql_insert(table).values(**values)
            # MySQL 的 on_duplicate_key_update 只接受字符串关键字，
            # 与 SQLite 的 Column 键不同。
            session.execute(
                stmt.on_duplicate_key_update(
                    **{column.name: expr for column, expr in set_.items()}
                )
            )
            return

        row = session.get(ControlUserUsageDaily, (tenant, user, day))
        if row is None:
            row = ControlUserUsageDaily(
                tenant=tenant, user=user, day=day
            )
            session.add(row)
            session.flush()
        for key, delta in adds.items():
            current = getattr(row, key)
            new = current + delta
            if key.startswith("reserved") and new < 0:
                new = 0
            setattr(row, key, new)

    def _insert_usage_event(
        self, session: Session, values: dict[str, Any]
    ) -> bool:
        """幂等插入 run_end 快照；返回是否真的插入了新行。"""
        table = ControlUsageEvent.__table__
        dialect = self.engine.dialect.name
        if dialect == "sqlite":
            stmt = sqlite_insert(table).values(**values)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[table.c.tenant, table.c.record_hash]
            )
            return bool(session.execute(stmt).rowcount)
        if dialect == "mysql":
            stmt = mysql_insert(table).prefix_with("IGNORE").values(**values)
            return bool(session.execute(stmt).rowcount)
        key = (str(values["tenant"]), str(values["record_hash"]))
        if session.get(ControlUsageEvent, key) is not None:
            return False
        session.add(ControlUsageEvent(**values))
        return True

    def record_usage(
        self, tenant: str, user: str, records: list[dict[str, Any]]
    ) -> int:
        """把新增的 run_end 审计快照累加到用户日用量。

        幂等键是审计 record_hash。resume 产生的 run_end 是同一 run_epoch
        的累计值，按 cursor 取差值，不能直接相加。

        - 快照用 INSERT ... ON CONFLICT/IGNORE 幂等插入，插入成功才累加；
        - 日用量走 `_daily_add` 的原子增量；
        - cursor 在 MySQL 上取行锁，进程内再由 `_USAGE_LOCK` 串行化。

        SQLite 没有行锁，两个进程可能**同时读到空游标**、各按全量累加。收敛
        靠的是：cursor 是复合主键，后写的一方必然撞唯一键 → 整个事务回滚
        （包括刚加的增量）→ 重试时读到对方已提交的游标，按差值补。

        所以下面那个 `except IntegrityError` 里的重试**不是兜底，是正确性
        的一部分**：删掉它就会静默多记一次。
        `test_quota_usage.py::test_concurrent_first_cursor_insert_does_not_double_count`
        专门守这条路径（它会拿掉 `_USAGE_LOCK` 并放大窗口来模拟并发进程）。
        """
        tenant = str(tenant)
        user = str(user)
        with _USAGE_LOCK:
            for attempt in range(2):
                applied = 0
                try:
                    with Session(self.engine) as session, session.begin():
                        for record in records:
                            applied += self._apply_usage_record(
                                session, tenant, user, record
                            )
                except IntegrityError:
                    # cursor 首次插入撞唯一键：另一个实例已经建好，重试一次
                    # 就会走行锁路径。第二次仍失败说明不是这个竞态，抛出。
                    if attempt:
                        raise
                    _LOG.warning(
                        "用量游标并发插入冲突，重试一次", exc_info=True
                    )
                else:
                    return applied
        return 0

    def _apply_usage_record(
        self,
        session: Session,
        tenant: str,
        user: str,
        record: dict[str, Any],
    ) -> int:
        if str(record.get("kind") or "") not in {"run_end", "run_error"}:
            return 0
        record_hash = str(record.get("hash") or "")
        if not record_hash:
            return 0
        epoch = str(record.get("run_epoch") or "")
        if not epoch:
            # 旧版 A3 没有 epoch，无法区分 resume 累计值，跳过以免多记。
            _LOG.debug("run_end 缺少 run_epoch，跳过计量")
            return 0
        thread_id = str(record.get("thread_id") or "")
        ts = str(record.get("ts") or "")
        day = ts[:10] if len(ts) >= 10 else _today()
        input_tokens = max(0, int(record.get("input_tokens") or 0))
        output_tokens = max(0, int(record.get("output_tokens") or 0))
        worker_in = max(0, int(record.get("worker_input_tokens") or 0))
        worker_out = max(0, int(record.get("worker_output_tokens") or 0))
        total = input_tokens + output_tokens + worker_in + worker_out
        cost = max(0.0, float(record.get("cost_usd") or 0))
        billed = max(0.0, float(record.get("billed_usd") or 0))

        inserted = self._insert_usage_event(
            session,
            {
                "tenant": tenant,
                "record_hash": record_hash,
                "user": user,
                "thread_id": thread_id,
                "run_epoch": epoch,
                "ts": ts,
                "tokens": total,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "worker_input_tokens": worker_in,
                "worker_output_tokens": worker_out,
                "cost_usd": cost,
                "billed_usd": billed,
            },
        )
        if not inserted:
            return 0

        cursor = session.scalar(
            select(ControlUsageCursor)
            .where(
                ControlUsageCursor.tenant == tenant,
                ControlUsageCursor.user == user,
                ControlUsageCursor.thread_id == thread_id,
            )
            .with_for_update()
        )
        same_epoch = cursor is not None and cursor.run_epoch == epoch
        if same_epoch and cursor is not None:
            delta_in = max(0, input_tokens - cursor.input_tokens)
            delta_out = max(0, output_tokens - cursor.output_tokens)
            delta_win = max(0, worker_in - cursor.worker_input_tokens)
            delta_wout = max(0, worker_out - cursor.worker_output_tokens)
            delta_cost = max(0.0, cost - cursor.cost_usd)
            delta_billed = max(0.0, billed - cursor.billed_usd)
        else:
            delta_in, delta_out = input_tokens, output_tokens
            delta_win, delta_wout = worker_in, worker_out
            delta_cost, delta_billed = cost, billed

        self._daily_add(
            session,
            tenant=tenant,
            user=user,
            day=day,
            input_tokens=delta_in,
            output_tokens=delta_out,
            worker_input_tokens=delta_win,
            worker_output_tokens=delta_wout,
            cost_usd=delta_cost,
            billed_usd=delta_billed,
            runs=0 if same_epoch else 1,
        )

        if cursor is None:
            cursor = ControlUsageCursor(
                tenant=tenant,
                user=user,
                thread_id=thread_id,
            )
            session.add(cursor)
        cursor.run_epoch = epoch
        cursor.tokens = total
        cursor.input_tokens = input_tokens
        cursor.output_tokens = output_tokens
        cursor.worker_input_tokens = worker_in
        cursor.worker_output_tokens = worker_out
        cursor.cost_usd = cost
        cursor.billed_usd = billed
        return 1

    def purge_usage_events(self, before_ts: str) -> int:
        """删除指定时间之前的 run_end 快照；日汇总不受影响。"""
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                ControlUsageEvent.__table__.delete().where(
                    ControlUsageEvent.ts < str(before_ts)
                )
            )
            return int(result.rowcount or 0)

    def purge_expired_reservations(self, now: str = "") -> int:
        """清理过期预留并归还对应额度；多实例各自执行也安全。

        每个预留先条件 DELETE，只有真正删到（rowcount=1）的实例才减
        reserved_*。两个实例同时清理同一行时，另一个会删到 0 行并跳过，
        不会把预留额度扣两次。
        """
        cutoff = str(now or _now())
        purged = 0
        with Session(self.engine) as session, session.begin():
            rows = session.scalars(
                select(ControlQuotaReservation).where(
                    ControlQuotaReservation.expires_at <= cutoff
                )
            ).all()
            for row in rows:
                deleted = session.execute(
                    delete(ControlQuotaReservation).where(
                        ControlQuotaReservation.id == row.id,
                        ControlQuotaReservation.expires_at <= cutoff,
                    )
                ).rowcount
                if not deleted:
                    continue
                purged += 1
                self._daily_add(
                    session,
                    tenant=row.tenant,
                    user=row.user,
                    day=row.day,
                    reserved_tokens=-int(row.tokens or 0),
                    reserved_usd=-float(row.cost_usd or 0.0),
                )
        return purged

    def get_quota(self, tenant: str, user: str) -> dict[str, Any]:
        with Session(self.engine) as session:
            row = session.get(ControlUserQuota, (str(tenant), str(user)))
            return _quota_row(row)

    def set_quota(
        self,
        tenant: str,
        user: str,
        *,
        daily_tokens: int,
        daily_cost_cny: float,
        enabled: bool,
        updated_by: str = "",
    ) -> dict[str, Any]:
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlUserQuota, (str(tenant), str(user)))
            if row is None:
                row = ControlUserQuota(tenant=str(tenant), user=str(user))
                session.add(row)
            row.daily_tokens = max(0, int(daily_tokens or 0))
            row.daily_cost_cny = max(0.0, float(daily_cost_cny or 0))
            row.enabled = bool(enabled)
            row.updated_by = str(updated_by or "")
            row.updated_at = _now()
            session.flush()
            return _quota_row(row)

    def get_usage_day(self, tenant: str, user: str, day: str) -> dict[str, Any]:
        with Session(self.engine) as session:
            row = session.get(
                ControlUserUsageDaily, (str(tenant), str(user), str(day))
            )
            return _usage_row(row, str(day))

    def usage_report(
        self,
        tenant: str,
        from_day: str,
        to_day: str,
        *,
        user: str | None = None,
        limit: int = 0,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions = [
            ControlUserUsageDaily.tenant == str(tenant),
            ControlUserUsageDaily.day >= str(from_day),
            ControlUserUsageDaily.day <= str(to_day),
        ]
        if user:
            conditions.append(ControlUserUsageDaily.user == str(user))
        with Session(self.engine) as session:
            aggregate = session.execute(
                select(
                    func.coalesce(
                        func.sum(ControlUserUsageDaily.input_tokens), 0
                    ),
                    func.coalesce(
                        func.sum(ControlUserUsageDaily.output_tokens), 0
                    ),
                    func.coalesce(
                        func.sum(
                            ControlUserUsageDaily.worker_input_tokens
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            ControlUserUsageDaily.worker_output_tokens
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(ControlUserUsageDaily.cost_usd), 0
                    ),
                    func.coalesce(
                        func.sum(ControlUserUsageDaily.billed_usd), 0
                    ),
                    func.coalesce(func.sum(ControlUserUsageDaily.runs), 0),
                    func.coalesce(
                        func.sum(ControlUserUsageDaily.reserved_tokens), 0
                    ),
                    func.coalesce(
                        func.sum(ControlUserUsageDaily.reserved_usd), 0
                    ),
                ).where(*conditions)
            ).one()
            query = (
                select(ControlUserUsageDaily)
                .where(*conditions)
                .order_by(
                    ControlUserUsageDaily.day.desc(),
                    ControlUserUsageDaily.user,
                )
            )
            if limit and int(limit) > 0:
                query = query.limit(int(limit))
            if offset and int(offset) > 0:
                query = query.offset(int(offset))
            rows = session.scalars(query).all()
        (
            input_tokens,
            output_tokens,
            worker_in,
            worker_out,
            cost_usd,
            billed_usd,
            runs,
            reserved_tokens,
            reserved_usd,
        ) = aggregate
        totals = {
            "day": "",
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "worker_input_tokens": int(worker_in or 0),
            "worker_output_tokens": int(worker_out or 0),
            "tokens": (
                int(input_tokens or 0)
                + int(output_tokens or 0)
                + int(worker_in or 0)
                + int(worker_out or 0)
            ),
            "cost_usd": float(cost_usd or 0.0),
            "billed_usd": float(billed_usd or 0.0),
            "runs": int(runs or 0),
            "reserved_tokens": int(reserved_tokens or 0),
            "reserved_usd": float(reserved_usd or 0.0),
        }
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _usage_row(row, row.day)
            item["user"] = row.user
            items.append(item)
        return {"rows": items, "totals": totals}

    @staticmethod
    def _quota_details(
        daily: ControlUserUsageDaily,
        *,
        reserve_tokens: int,
        reserve_cost: float,
        limit_tokens: int,
        limit_cost_cny: float,
        rate: float,
        day: str,
    ) -> dict[str, Any] | None:
        used_tokens = (
            daily.input_tokens
            + daily.output_tokens
            + daily.worker_input_tokens
            + daily.worker_output_tokens
        )
        if limit_tokens > 0 and (
            used_tokens + daily.reserved_tokens + reserve_tokens
            > limit_tokens
        ):
            return {
                "kind": "tokens",
                "limit": limit_tokens,
                "used": used_tokens + daily.reserved_tokens,
                "day": day,
            }
        used_cost_cny = (
            (daily.cost_usd + daily.reserved_usd) * rate
        )
        if limit_cost_cny > 0 and (
            used_cost_cny + reserve_cost * rate > limit_cost_cny
        ):
            return {
                "kind": "cost_cny",
                "limit": limit_cost_cny,
                "used": used_cost_cny,
                "day": day,
            }
        return None

    def _reserve_check_only(
        self,
        session: Session,
        *,
        tenant: str,
        user: str,
        day: str,
        limit_tokens: int,
        limit_cost_cny: float,
        rate: float,
    ) -> None:
        """`reserve=0` 的运行前检查：只读已用额度，不做预留。"""
        daily = session.scalar(
            select(ControlUserUsageDaily)
            .where(
                ControlUserUsageDaily.tenant == tenant,
                ControlUserUsageDaily.user == user,
                ControlUserUsageDaily.day == day,
            )
            .with_for_update()
        )
        if daily is None:
            # 用方言 upsert 建行，避免两个并发 run 同时插入撞唯一键。
            self._daily_add(
                session, tenant=tenant, user=user, day=day
            )
            return
        details = self._quota_details(
            daily,
            reserve_tokens=0,
            reserve_cost=0.0,
            limit_tokens=limit_tokens,
            limit_cost_cny=limit_cost_cny,
            rate=rate,
            day=day,
        )
        if details is not None:
            raise QuotaExceeded(details)

    def _reserve_atomic(
        self,
        session: Session,
        *,
        tenant: str,
        user: str,
        day: str,
        reserve_tokens: int,
        reserve_cost: float,
        limit_tokens: int,
        limit_cost_cny: float,
        rate: float,
    ) -> None:
        """条件 UPDATE 完成“检查 + 自增”；行不存在时插入并处理首次竞态。"""
        table = ControlUserUsageDaily.__table__
        used_tokens = (
            table.c.input_tokens
            + table.c.output_tokens
            + table.c.worker_input_tokens
            + table.c.worker_output_tokens
        )
        used_cost = (
            table.c.cost_usd
            + table.c.reserved_usd
            + reserve_cost
        ) * rate
        stmt = (
            update(table)
            .where(
                table.c.tenant == tenant,
                table.c.user == user,
                table.c.day == day,
                or_(
                    limit_tokens <= 0,
                    used_tokens
                    + table.c.reserved_tokens
                    + reserve_tokens
                    <= limit_tokens,
                ),
                or_(
                    limit_cost_cny <= 0,
                    used_cost <= limit_cost_cny,
                ),
            )
            .values(
                reserved_tokens=table.c.reserved_tokens + reserve_tokens,
                reserved_usd=table.c.reserved_usd + reserve_cost,
            )
        )
        for attempt in range(2):
            if session.execute(stmt).rowcount:
                return
            daily = session.get(
                ControlUserUsageDaily, (tenant, user, day)
            )
            if daily is not None:
                details = self._quota_details(
                    daily,
                    reserve_tokens=reserve_tokens,
                    reserve_cost=reserve_cost,
                    limit_tokens=limit_tokens,
                    limit_cost_cny=limit_cost_cny,
                    rate=rate,
                    day=day,
                )
                if details is not None:
                    raise QuotaExceeded(details)
                continue
            if (
                limit_tokens > 0 and reserve_tokens > limit_tokens
            ) or (
                limit_cost_cny > 0
                and reserve_cost * rate > limit_cost_cny
            ):
                details = self._quota_details(
                    ControlUserUsageDaily(
                        tenant=tenant, user=user, day=day
                    ),
                    reserve_tokens=reserve_tokens,
                    reserve_cost=reserve_cost,
                    limit_tokens=limit_tokens,
                    limit_cost_cny=limit_cost_cny,
                    rate=rate,
                    day=day,
                )
                raise QuotaExceeded(
                    details
                    or {
                        "kind": "tokens",
                        "limit": limit_tokens,
                        "used": 0,
                        "day": day,
                    }
                )
            try:
                with session.begin_nested():
                    session.add(
                        ControlUserUsageDaily(
                            tenant=tenant,
                            user=user,
                            day=day,
                            reserved_tokens=reserve_tokens,
                            reserved_usd=reserve_cost,
                        )
                    )
                    session.flush()
            except IntegrityError:
                if attempt:
                    raise
                continue
            else:
                return
        raise QuotaExceeded(
            {
                "kind": "tokens",
                "limit": limit_tokens,
                "used": 0,
                "day": day,
            }
        )

    def reserve_quota(
        self,
        tenant: str,
        user: str,
        day: str,
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
        usd_to_cny: float = 7.2,
        owner: str = "",
        ttl_s: int = 3600,
    ) -> dict[str, Any]:
        """原子预留；条件不满足时抛 QuotaExceeded。

        判定与 `reserved_*` 自增落在同一条条件 UPDATE 里：SQLite 忽略行锁
        也不会丢更新；MySQL 用行锁串行化。`reserve=0` 时只做运行前检查，
        不做预留（默认配置），此时不依赖 UPDATE 的 rowcount（MySQL 只统计
        实际变更行）。
        """
        tenant = str(tenant)
        user = str(user)
        day_text = str(day)
        reserve_tokens = max(0, int(tokens or 0))
        reserve_cost = max(0.0, float(cost_usd or 0))
        rate = float(usd_to_cny or 1)
        # 先清理过期预留，再进入预留事务。
        self.purge_expired_reservations()
        with Session(self.engine) as session, session.begin():
            quota = session.get(ControlUserQuota, (tenant, user))
            quota_data = _quota_row(quota)
            if not quota_data["enabled"]:
                return {"id": "", "tokens": 0, "cost_usd": 0.0}
            limit_tokens = int(quota_data["daily_tokens"] or 0)
            limit_cost_cny = float(quota_data["daily_cost_cny"] or 0)

            if reserve_tokens == 0 and reserve_cost == 0:
                self._reserve_check_only(
                    session,
                    tenant=tenant,
                    user=user,
                    day=day_text,
                    limit_tokens=limit_tokens,
                    limit_cost_cny=limit_cost_cny,
                    rate=rate,
                )
            else:
                self._reserve_atomic(
                    session,
                    tenant=tenant,
                    user=user,
                    day=day_text,
                    reserve_tokens=reserve_tokens,
                    reserve_cost=reserve_cost,
                    limit_tokens=limit_tokens,
                    limit_cost_cny=limit_cost_cny,
                    rate=rate,
                )

            reservation = ControlQuotaReservation(
                id=uuid4().hex,
                tenant=tenant,
                user=user,
                day=day_text,
                tokens=reserve_tokens,
                cost_usd=reserve_cost,
                owner=str(owner or "")[:64],
                expires_at=_future(ttl_s),
                created_at=_now(),
            )
            session.add(reservation)
            return {
                "id": reservation.id,
                "tokens": reserve_tokens,
                "cost_usd": reserve_cost,
                "day": day_text,
            }

    def settle_quota(self, reservation_id: str) -> None:
        """按预留 id 释放；实际用量由审计累加，不在这里重复记账。

        先删预留行，只有删到（rowcount=1）的调用才减少 reserved_*，
        并发结算同一 id 时不会重复扣减。
        """
        rid = str(reservation_id or "")
        if not rid:
            return
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlQuotaReservation, rid)
            if row is None:
                return
            deleted = session.execute(
                delete(ControlQuotaReservation).where(
                    ControlQuotaReservation.id == rid
                )
            ).rowcount
            if not deleted:
                return
            self._daily_add(
                session,
                tenant=row.tenant,
                user=row.user,
                day=row.day,
                reserved_tokens=-int(row.tokens or 0),
                reserved_usd=-float(row.cost_usd or 0.0),
            )


def _quota_row(row: Any) -> dict[str, Any]:
    if row is None:
        return {
            "daily_tokens": 0,
            "daily_cost_cny": 0.0,
            "enabled": False,
            "updated_by": "",
            "updated_at": "",
        }
    return {
        "daily_tokens": int(row.daily_tokens or 0),
        "daily_cost_cny": float(row.daily_cost_cny or 0.0),
        "enabled": bool(row.enabled),
        "updated_by": str(row.updated_by or ""),
        "updated_at": str(row.updated_at or ""),
    }


def _usage_row(row: Any, day: str) -> dict[str, Any]:
    if row is None:
        return {
            "day": str(day),
            "input_tokens": 0,
            "output_tokens": 0,
            "worker_input_tokens": 0,
            "worker_output_tokens": 0,
            "tokens": 0,
            "cost_usd": 0.0,
            "billed_usd": 0.0,
            "runs": 0,
            "reserved_tokens": 0,
            "reserved_usd": 0.0,
        }
    input_tokens = int(row.input_tokens or 0)
    output_tokens = int(row.output_tokens or 0)
    worker_input = int(row.worker_input_tokens or 0)
    worker_output = int(row.worker_output_tokens or 0)
    return {
        "day": str(day),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "worker_input_tokens": worker_input,
        "worker_output_tokens": worker_output,
        "tokens": input_tokens + output_tokens + worker_input + worker_output,
        "cost_usd": float(row.cost_usd or 0.0),
        "billed_usd": float(row.billed_usd or 0.0),
        "runs": int(row.runs or 0),
        "reserved_tokens": int(row.reserved_tokens or 0),
        "reserved_usd": float(row.reserved_usd or 0.0),
    }
