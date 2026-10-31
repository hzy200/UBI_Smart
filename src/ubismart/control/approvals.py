"""四眼审批事实（RFC-0004）。

A2 在 SSE 里看到 `approval_requested` 时落一行；发起人的 `/api/resume`
只能对低等级调用使用自己的决定，高等级调用只认管理员队列里的决定。
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coding_agent.control.schema import ControlApproval
from coding_agent.security.secrets import redact_secrets


class ApprovalError(RuntimeError):
    pass


class SelfApprovalError(ApprovalError):
    """发起人试图批准自己的高等级审批。"""


def _now() -> str:
    return str(int(time.time()))


def _iso(value: str) -> str:
    """epoch 秒 → ISO；空串/非法值原样返回。"""
    try:
        return datetime.fromtimestamp(
            int(str(value)), UTC
        ).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return str(value or "")


def _public(row: ControlApproval) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant": row.tenant,
        "thread_id": row.thread_id,
        "call_id": row.call_id,
        "tool": row.tool,
        "command": row.command,
        "level": row.level,
        "level_int": int(row.level_int or 0),
        "requested_by": row.requested_by,
        "status": row.status,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        # 前端显示用；原 epoch 字段保留给旧调用方。
        "created_at_iso": _iso(row.created_at),
        "decided_at_iso": _iso(row.decided_at),
        "expires_at_iso": _iso(row.expires_at),
    }


class ApprovalStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def record_pending(
        self,
        *,
        tenant: str,
        thread_id: str,
        call_id: str,
        tool: str,
        command: str,
        level: str,
        level_int: int,
        requested_by: str,
        ttl_s: int = 3600,
    ) -> None:
        if not call_id or level_int <= 0:
            return
        now = int(time.time())
        with Session(self.engine) as session, session.begin():
            existing = session.scalar(
                select(ControlApproval).where(
                    ControlApproval.tenant == str(tenant),
                    ControlApproval.thread_id == str(thread_id),
                    ControlApproval.call_id == str(call_id),
                )
            )
            if existing is not None:
                if existing.status == "pending":
                    existing.command = redact_secrets(str(command or ""))[:4000]
                    existing.level = str(level or "")[:32]
                    existing.level_int = int(level_int)
                return
            try:
                with session.begin_nested():
                    session.add(
                        ControlApproval(
                            id=uuid.uuid4().hex,
                            tenant=str(tenant),
                            thread_id=str(thread_id)[:128],
                            call_id=str(call_id)[:128],
                            tool=str(tool or "")[:64],
                            command=redact_secrets(
                                str(command or "")
                            )[:4000],
                            level=str(level or "")[:32],
                            level_int=int(level_int),
                            requested_by=str(requested_by or "")[:64],
                            status="pending",
                            decided_by="",
                            decided_at="",
                            created_at=_now(),
                            expires_at=str(now + max(60, int(ttl_s))),
                        )
                    )
                    session.flush()
            except IntegrityError:
                # 同一 call_id 被并发记录：交给已有行。
                pass

    def decide(
        self,
        *,
        tenant: str,
        approval_id: str,
        approver: str,
        approved: bool,
    ) -> dict[str, Any] | None:
        now = _now()
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlApproval, str(approval_id))
            if row is None or row.tenant != str(tenant):
                return None
            if row.requested_by == str(approver):
                raise SelfApprovalError("发起人不能批准自己的审批请求")
            status = "approved" if approved else "denied"
            updated = session.execute(
                update(ControlApproval)
                .where(
                    ControlApproval.id == str(approval_id),
                    ControlApproval.status == "pending",
                    ControlApproval.requested_by != str(approver),
                )
                .values(
                    status=status,
                    decided_by=str(approver or "")[:64],
                    decided_at=now,
                )
            ).rowcount
            if not updated:
                session.refresh(row)
                return _public(row)
            session.refresh(row)
            return _public(row)

    @staticmethod
    def _merged_value(
        row: ControlApproval,
        owner_decision: bool | None,
        min_level: int,
    ) -> bool | None:
        """返回该 call_id 的有效决定；None = 不参与合并（protected/pending）。"""
        if int(row.level_int or 0) < min_level:
            return owner_decision
        if (
            row.status in {"approved", "denied"}
            and row.decided_by
            and row.decided_by != row.requested_by
        ):
            return row.status == "approved"
        return None

    def merge_decisions(
        self,
        *,
        tenant: str,
        thread_id: str,
        owner_decisions: dict[str, bool],
        min_level: int,
    ) -> dict[str, bool]:
        """合并发起人决定与管理员决定。

        - 高等级行只认 `status in (approved, denied)` 且非发起人的决定；
        - 仍在 pending 的高等级行从结果中剔除（A3 会按拒绝处理）；
        - 未知 call_id 在四眼开启时也剔除，fail closed。
        """
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlApproval).where(
                    ControlApproval.tenant == str(tenant),
                    ControlApproval.thread_id == str(thread_id),
                )
            ).all()
        if not rows:
            # 四眼开启时没有审批事实就是不可判定的，fail closed。
            return {} if min_level > 0 else dict(owner_decisions)
        by_call = {row.call_id: row for row in rows}
        merged: dict[str, bool] = {}
        for call_id, decision in owner_decisions.items():
            row = by_call.get(str(call_id))
            if row is None:
                if min_level <= 0:
                    merged[str(call_id)] = bool(decision)
                continue
            value = self._merged_value(row, bool(decision), min_level)
            if value is not None:
                merged[str(call_id)] = value
        # 管理员已决定、但发起人没有在本次请求里提交的 call_id。
        for call_id, row in by_call.items():
            if call_id in merged:
                continue
            value = self._merged_value(row, None, min_level)
            if value is not None:
                merged[call_id] = value
        return merged

    def pending(
        self, tenant: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlApproval)
                .where(
                    ControlApproval.tenant == str(tenant),
                    ControlApproval.status == "pending",
                )
                .order_by(ControlApproval.created_at.desc())
                .limit(max(1, int(limit)))
            ).all()
            return [_public(row) for row in rows]

    def history(
        self,
        tenant: str,
        *,
        status: str = "",
        requested_by: str = "",
        from_ts: str = "",
        to_ts: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """审批历史；`from_ts`/`to_ts` 是 epoch 秒字符串。"""
        conditions = [ControlApproval.tenant == str(tenant)]
        if status:
            conditions.append(ControlApproval.status == str(status))
        if requested_by:
            conditions.append(
                ControlApproval.requested_by == str(requested_by)
            )
        if from_ts:
            conditions.append(ControlApproval.created_at >= str(from_ts))
        if to_ts:
            conditions.append(ControlApproval.created_at <= str(to_ts))
        size = max(1, min(500, int(limit)))
        start = max(0, int(offset))
        with Session(self.engine) as session:
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(ControlApproval)
                    .where(*conditions)
                )
                or 0
            )
            rows = session.scalars(
                select(ControlApproval)
                .where(*conditions)
                .order_by(
                    ControlApproval.created_at.desc(),
                    ControlApproval.id.desc(),
                )
                .offset(start)
                .limit(size)
            ).all()
        records = [_public(row) for row in rows]
        return {
            "records": records,
            "total": total,
            "has_more": start + len(records) < total,
        }

    def purge_expired(self) -> int:
        now = int(time.time())
        with Session(self.engine) as session, session.begin():
            return int(
                session.execute(
                    delete(ControlApproval).where(
                        ControlApproval.expires_at < str(now),
                        ControlApproval.status == "pending",
                    )
                ).rowcount
                or 0
            )
