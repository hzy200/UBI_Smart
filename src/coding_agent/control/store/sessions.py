"""会话目录与工作区绑定。

从 `control/store.py` 拆出的聚合根 mixin；方法体未改动。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from coding_agent.control.schema import ControlSession, Workspace

from .base import _now, _tags_from_text, _tags_to_text


class SessionsMixin:
    """`ControlStore` 的会话目录与工作区绑定。"""

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
