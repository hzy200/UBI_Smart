"""用量记账：游标、日汇总与审计快照。

从 `store/usage.py` 拆出的 mixin；方法体未改动。
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ubismart.control.schema import (
    ControlUsageCursor,
    ControlUsageEvent,
    ControlUserUsageDaily,
)

from .base import _LOG, _today

_USAGE_LOCK = threading.Lock()


class UsageRecordMixin:
    """`UsageMixin` 的用量记账：游标、日汇总与审计快照。"""

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
