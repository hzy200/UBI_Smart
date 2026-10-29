"""用量记账、配额预留与报表。

从 `control/store.py` 拆出的聚合根 mixin；方法体未改动。
"""

from __future__ import annotations

import threading
from typing import Any
from uuid import uuid4

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coding_agent.control.schema import (
    ControlQuotaReservation,
    ControlUsageCursor,
    ControlUsageEvent,
    ControlUserQuota,
    ControlUserUsageDaily,
)

from .base import _LOG, QuotaExceeded, _future, _now, _today

_USAGE_LOCK = threading.Lock()

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


class UsageMixin:
    """`ControlStore` 的用量记账、配额预留与报表。"""

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
