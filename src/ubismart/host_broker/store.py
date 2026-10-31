"""A2L 本地工作区映射：workspace_id ↔ absolute_root。

绝对路径只留在客户端；A2 只拿 `workspace_id + root_handle + a2l_id`。
A2L 重启后必须能恢复用户登记过的目录，否则 A3 发来的 host 读取会
报 `WORKSPACE_HANDLE_UNKNOWN`。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_roots (
    workspace_id TEXT PRIMARY KEY,
    absolute_root TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    root_handle TEXT NOT NULL DEFAULT '',
    a2l_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
)
"""


def default_store_path() -> Path:
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        ) / "CodingAgent"
    else:
        base = Path.home() / ".coding-agent"
    return base / "a2l-workspaces.sqlite3"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorkspaceRootStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def load(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT workspace_id, absolute_root FROM workspace_roots"
            ).fetchall()
        return {
            str(workspace_id): str(root)
            for workspace_id, root in rows
            if workspace_id and root
        }

    def upsert(
        self,
        workspace_id: str,
        absolute_root: str,
        *,
        display_name: str = "",
        root_handle: str = "",
        a2l_id: str = "",
    ) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workspace_roots (
                    workspace_id, absolute_root, display_name,
                    root_handle, a2l_id, created_at, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    absolute_root = excluded.absolute_root,
                    display_name = excluded.display_name,
                    root_handle = excluded.root_handle,
                    a2l_id = excluded.a2l_id,
                    last_seen = excluded.last_seen
                """,
                (
                    str(workspace_id),
                    str(absolute_root),
                    str(display_name or ""),
                    str(root_handle or ""),
                    str(a2l_id or ""),
                    now,
                    now,
                ),
            )

    def remove(self, workspace_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM workspace_roots WHERE workspace_id = ?",
                (str(workspace_id),),
            )
