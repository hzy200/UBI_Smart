"""配额预留、结算与清理。

从 `store/usage.py` 拆出的 mixin；方法体未改动。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ubismart.control.schema import (
    ControlQuotaReservation,
    ControlUsageEvent,
    ControlUserQuota,
    ControlUserUsageDaily,
)

from .base import QuotaExceeded, _future, _now
from .usage_common import _quota_row
from .usage_report import _usage_row


class UsageReserveMixin:
    """`UsageMixin` 的配额预留、结算与清理。"""

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
