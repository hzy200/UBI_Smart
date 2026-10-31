"""用量报表与配额详情。

从 `store/usage.py` 拆出的 mixin；方法体未改动。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ubismart.control.schema import ControlUserUsageDaily


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


class UsageReportMixin:
    """`UsageMixin` 的用量报表与配额详情。"""

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
