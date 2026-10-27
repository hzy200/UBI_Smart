"""A2L 本地操作日志：崩溃恢复时区分“没做”和“结果未知”。

Phase 1 只读操作重试是安全的，但仍要求先落 `op_id`。Phase 2 的写/执行
必须在恢复时把未完成的 op 标成 `unknown`，由 A3 决定验证或终止，
**不能盲目重放**。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OP_STARTED = "started"
OP_FINISHED = "finished"
OP_FAILED = "failed"
OP_CANCELLED = "cancelled"
OP_UNKNOWN = "unknown"

_TERMINAL = frozenset({OP_FINISHED, OP_FAILED, OP_CANCELLED})


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class OpRecord:
    op_id: str
    kind: str
    status: str
    started_at: str
    finished_at: str = ""
    result: str = ""
    meta: str = ""


class OpJournal:
    """SQLite 单文件日志；A2L 单实例使用，不做跨进程并发写。"""

    def __init__(self, path: str | Path, *, recover: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()
        if recover:
            self.recover_started()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops (
                    op_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT '',
                    meta TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_ops_status ON ops(status)"
            )
            conn.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> OpRecord | None:
        if row is None:
            return None
        return OpRecord(
            op_id=str(row["op_id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            started_at=str(row["started_at"]),
            finished_at=str(row["finished_at"]),
            result=str(row["result"]),
            meta=str(row["meta"]),
        )

    def start(
        self,
        op_id: str,
        kind: str,
        *,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        """记录一次操作开始；同一 op_id 已存在时返回 False（幂等）。"""
        op_id = str(op_id or "").strip()
        if not op_id:
            raise ValueError("op_id 不能为空")
        with self._lock, closing(self._connect()) as conn:
            cur = conn.execute(
                """
                INSERT INTO ops(op_id, kind, status, started_at, meta)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(op_id) DO NOTHING
                """,
                (
                    op_id,
                    str(kind or ""),
                    OP_STARTED,
                    _now(),
                    json.dumps(meta or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def finish(
        self,
        op_id: str,
        status: str = OP_FINISHED,
        *,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """写入终态；只允许 finished/failed/cancelled/unknown。"""
        if status not in _TERMINAL and status != OP_UNKNOWN:
            raise ValueError(f"非法终态：{status!r}")
        with self._lock, closing(self._connect()) as conn:
            cur = conn.execute(
                """
                UPDATE ops
                   SET status = ?, finished_at = ?, result = ?
                 WHERE op_id = ? AND status IN (?, ?)
                """,
                (
                    status,
                    _now(),
                    json.dumps(result or {}, ensure_ascii=False, sort_keys=True),
                    str(op_id or "").strip(),
                    OP_STARTED,
                    OP_UNKNOWN,
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def get(self, op_id: str) -> OpRecord | None:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM ops WHERE op_id = ?",
                (str(op_id or "").strip(),),
            ).fetchone()
        return self._row(row)

    def recover_started(self) -> int:
        """进程重启后把遗留的 started 标成 unknown，返回受影响行数。"""
        with self._lock, closing(self._connect()) as conn:
            cur = conn.execute(
                """
                UPDATE ops
                   SET status = ?, finished_at = ?
                 WHERE status = ?
                """,
                (OP_UNKNOWN, _now(), OP_STARTED),
            )
            conn.commit()
            return cur.rowcount

    def unknown(self) -> list[OpRecord]:
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM ops WHERE status = ? ORDER BY started_at",
                (OP_UNKNOWN,),
            ).fetchall()
        return [record for row in rows if (record := self._row(row)) is not None]
