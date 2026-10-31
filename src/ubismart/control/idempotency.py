"""短期幂等键（RFC-0001，M4）。

只用于 A2 的 JSON 写操作（工作区创建/导入/上传/回滚）。
SSE 运行使用租约防止重复启动，不在这里重放事件流。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ubismart.control.schema import IdempotencyKey


class IdempotencyInFlight(RuntimeError):
    """同一个 key 已有请求在执行。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def normalize_key(key: str) -> str:
    text = str(key or "").strip()
    if not text:
        return ""
    if len(text) > 128:
        raise ValueError("Idempotency-Key 过长（最多 128）")
    return text


class IdempotencyStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def begin(
        self, key: str, scope: str, *, ttl_s: int = 600
    ) -> dict | None:
        """返回已完成的响应；None 表示本次请求需要执行。"""
        text = normalize_key(key)
        if not text:
            return None
        now = _now()
        now_text = _fmt(now)
        expires = _fmt(now + timedelta(seconds=max(1, int(ttl_s))))
        # 同 `control/leases.py`：`SELECT ... FOR UPDATE` 在 SQLite 上是空操作，
        # 读-改-写不是原子的。抢占改成"条件 UPDATE"，判定与写入一条语句完成。
        claim = (
            update(IdempotencyKey)
            .where(
                IdempotencyKey.key == text,
                IdempotencyKey.expires_at <= now_text,
            )
            .values(
                scope=str(scope or ""),
                status="running",
                response="",
                expires_at=expires,
            )
        )
        try:
            with Session(self.engine) as session, session.begin():
                if session.execute(claim).rowcount:
                    return None
                row = session.get(IdempotencyKey, text)
                if row is None:
                    session.add(
                        IdempotencyKey(
                            key=text,
                            scope=str(scope or ""),
                            status="running",
                            response="",
                            expires_at=expires,
                            created_at=now_text,
                        )
                    )
                    return None
                if row.status == "done":
                    try:
                        payload = json.loads(row.response or "{}")
                    except ValueError:
                        payload = {}
                    return payload if isinstance(payload, dict) else {}
                raise IdempotencyInFlight(
                    f"幂等键 {text} 已有请求在执行"
                )
        except IntegrityError as exc:
            # 并发插入撞主键：fail closed —— 当作已有请求在执行。
            raise IdempotencyInFlight(
                f"幂等键 {text} 已有请求在执行"
            ) from exc

    def finish(
        self, key: str, response: dict, *, ttl_s: int = 600
    ) -> None:
        text = normalize_key(key)
        if not text:
            return
        now = _now()
        with Session(self.engine) as session, session.begin():
            row = session.get(IdempotencyKey, text)
            if row is None:
                session.add(
                    IdempotencyKey(
                        key=text,
                        scope="",
                        status="done",
                        response=json.dumps(
                            response, ensure_ascii=False
                        ),
                        expires_at=_fmt(
                            now + timedelta(seconds=max(1, int(ttl_s)))
                        ),
                        created_at=_fmt(now),
                    )
                )
                return
            row.status = "done"
            row.response = json.dumps(response, ensure_ascii=False)
            row.expires_at = _fmt(
                now + timedelta(seconds=max(1, int(ttl_s)))
            )

    def abort(self, key: str) -> None:
        text = normalize_key(key)
        if not text:
            return
        with Session(self.engine) as session, session.begin():
            row = session.get(IdempotencyKey, text)
            if row is not None and row.status == "running":
                session.delete(row)

    def purge_expired(self) -> int:
        statement = delete(IdempotencyKey).where(
            IdempotencyKey.expires_at <= _fmt(_now())
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            return int(result.rowcount or 0)
