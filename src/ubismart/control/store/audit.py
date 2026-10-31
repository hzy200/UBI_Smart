"""审计索引的写入与查询。

从 `control/store.py` 拆出的聚合根 mixin；方法体未改动。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ubismart.control.schema import ControlAuditIndex

from .base import _LOG


class AuditMixin:
    """`ControlStore` 的审计索引的写入与查询。"""

    def index_audit(
        self,
        tenant: str,
        records: list[dict[str, Any]],
        *,
        usage_tenant: str = "",
        usage_user: str = "",
    ) -> int:
        """把 A3 拉回来的审计记录幂等写入查询索引。

        只查询本批记录的 hash，不拉取该租户的全部历史。此前每次调用都会
        把整张索引表的 hash 读进内存，成本随累计审计量线性增长；审计查询
        和每次运行结束都会走到这里，长跑服务上会越来越慢。
        """
        added = 0
        hashes = {
            str(record.get("hash") or "") for record in records
        } - {""}
        known: set[str] = set()
        with Session(self.engine) as session, session.begin():
            # SQLite 的绑定参数上限是 999；按 500 分块，MySQL 同样安全。
            ordered = sorted(hashes)
            for offset in range(0, len(ordered), 500):
                known.update(
                    session.scalars(
                        select(ControlAuditIndex.record_hash).where(
                            ControlAuditIndex.tenant == str(tenant),
                            ControlAuditIndex.record_hash.in_(
                                ordered[offset : offset + 500]
                            ),
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
        if added and usage_tenant and usage_user:
            # 用量单独事务：失败不能回滚审计索引，下一次终态还会补。
            try:
                self.record_usage(
                    usage_tenant, usage_user, records
                )
            except Exception:  # noqa: BLE001 - 计量失败不阻断审计
                _LOG.warning("用量累加失败", exc_info=True)
        return added

    def list_audit(
        self,
        tenants: list[str],
        *,
        user: str = "",
        thread_id: str = "",
        kind: str = "",
        tool: str = "",
        ok: bool | None = None,
        since: str = "",
        until: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按组织成员查询审计索引。

        索引里的 `tenant` 是用户名（与会话/用量作用域一致）；调用方必须先
        用组织成员列表收窄范围，不能把任意用户名传进来。
        """
        scope = sorted({str(item) for item in tenants if str(item)})
        if user:
            scope = [str(user)] if str(user) in scope else []
        if not scope:
            return {"records": [], "total": 0, "has_more": False}
        query = select(ControlAuditIndex).where(
            ControlAuditIndex.tenant.in_(scope)
        )
        if thread_id:
            query = query.where(ControlAuditIndex.thread_id == str(thread_id))
        if kind:
            query = query.where(ControlAuditIndex.kind == str(kind))
        if tool:
            query = query.where(ControlAuditIndex.tool == str(tool))
        if ok is not None:
            query = query.where(ControlAuditIndex.ok.is_(bool(ok)))
        if since:
            query = query.where(ControlAuditIndex.ts >= str(since))
        if until:
            query = query.where(ControlAuditIndex.ts <= str(until))
        with Session(self.engine) as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(query.subquery())
                )
                or 0
            )
            rows = session.scalars(
                query.order_by(
                    ControlAuditIndex.ts.desc(),
                    ControlAuditIndex.id.desc(),
                )
                .offset(max(0, int(offset)))
                .limit(max(1, int(limit)))
            ).all()
        records = [
            {
                "tenant": row.tenant,
                "thread_id": row.thread_id,
                "ts": row.ts,
                "kind": row.kind,
                "tool": row.tool,
                "level": row.level,
                "decision": row.decision,
                "ok": row.ok,
                "record_hash": row.record_hash,
            }
            for row in rows
        ]
        return {
            "records": records,
            "total": total,
            "has_more": int(offset) + len(records) < total,
        }
