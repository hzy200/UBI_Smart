"""连接、schema 与健康检查。

从 `control/store.py` 拆出的聚合根 mixin；方法体未改动。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ubismart.control.schema import (
    CONTROL_SCHEMA_VERSION,
    Base,
    ControlMeta,
)

_LOG = logging.getLogger(__name__)

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


class BaseMixin:
    """`ControlStore` 的连接、schema 与健康检查。"""

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

    def ping(self) -> bool:
        """轻量连通性检查；供健康端点与降级判断使用。"""
        try:
            with self.engine.connect() as session:
                session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - 数据库故障是预期状态
            return False
        else:
            return True

    def close(self) -> None:
        self.engine.dispose()
