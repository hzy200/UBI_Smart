"""A2 控制面的会话目录存储（Phase B1/B2）。

`AGENT_CONTROL_DB_URL` 为空时本模块不参与运行；设置后 Web 控制面先把
`gui-sessions.json` 里已有的会话导入一次，再以数据库为准。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from coding_agent.control.schema import (
    CONTROL_SCHEMA_VERSION,
    Base,
    ControlAuditIndex,
    ControlMeta,
    ControlSession,
    Workspace,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        if auto_create:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        """首次建表；只创建缺失表，不做列迁移。"""
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session, session.begin():
            if session.get(ControlMeta, "schema_version") is None:
                session.add(
                    ControlMeta(
                        key="schema_version", value=CONTROL_SCHEMA_VERSION
                    )
                )

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
        self, tenant: str, records: list[dict[str, Any]]
    ) -> int:
        """把 A3 拉回来的审计记录幂等写入查询索引。"""
        added = 0
        with Session(self.engine) as session, session.begin():
            known = set(
                session.scalars(
                    select(ControlAuditIndex.record_hash).where(
                        ControlAuditIndex.tenant == str(tenant),
                        ControlAuditIndex.record_hash != "",
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
        return added

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
