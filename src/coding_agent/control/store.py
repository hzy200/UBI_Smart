"""A2 控制面的会话目录存储（Phase B1/B2）。

`AGENT_CONTROL_DB_URL` 为空时本模块不参与运行；设置后 Web 控制面先把
`gui-sessions.json` 里已有的会话导入一次，再以数据库为准。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from coding_agent.control.schema import (
    CONTROL_SCHEMA_VERSION,
    Base,
    ControlAuditIndex,
    ControlMeta,
    ControlQuotaReservation,
    ControlSession,
    ControlUsageCursor,
    ControlUsageEvent,
    ControlUserQuota,
    ControlUserUsageDaily,
    Workspace,
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


class ControlStore:
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
        if auto_create:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        """首次建表；只创建缺失表，不做列迁移。"""
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session, session.begin():
            if session.get(ControlMeta, "schema_version") is None:
                session.add(
                    ControlMeta(
                        key="schema_version", value=CONTROL_SCHEMA_VERSION
                    )
                )

    def load_sessions(
        self, tenant: str
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """返回（未删除会话，已删除 thread 集合）。"""
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlSession).where(
                    ControlSession.tenant == str(tenant)
                )
            ).all()
        active: list[dict[str, Any]] = []
        deleted: set[str] = set()
        for row in rows:
            if row.deleted:
                deleted.add(row.thread_id)
                continue
            active.append(
                {
                    "thread_id": row.thread_id,
                    "workspace": row.workspace,
                    "workspace_id": row.workspace_id,
                    "workspace_kind": row.workspace_kind,
                    "a2l_id": row.a2l_id,
                    "root_handle": row.root_handle,
                    "access_mode": row.access_mode,
                    "model": row.model,
                    "provider": row.provider,
                    "uploaded_bytes": int(row.uploaded_bytes or 0),
                    "title": row.title,
                    "pinned": bool(row.pinned),
                    "archived": bool(row.archived),
                    "tags": _tags_from_text(row.tags),
                    "unread": bool(row.unread),
                    "last_message": row.last_message,
                }
            )
        return active, deleted

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        """读取工作区元数据；会话配置重建时用它恢复 host 绑定。"""
        key = str(workspace_id or "")
        if not key:
            return None
        with Session(self.engine) as session:
            row = session.get(Workspace, key)
            if row is None:
                return None
            return {
                "workspace_id": row.workspace_id,
                "display_name": row.display_name,
                "kind": row.kind,
                "a2l_id": row.a2l_id,
                "root_handle": row.root_handle,
                "root_path": row.root_path,
                "status": row.status,
            }

    def save_sessions(
        self,
        tenant: str,
        sessions: list[dict[str, Any]],
        deleted: set[str],
    ) -> None:
        """在一个事务里 upsert 会话并写入 tombstone。"""
        now = _now()
        with Session(self.engine) as session, session.begin():
            for data in sessions:
                thread_id = str(data.get("thread_id") or "")
                if not thread_id:
                    continue
                row = session.get(ControlSession, (str(tenant), thread_id))
                if row is None:
                    row = ControlSession(
                        tenant=str(tenant),
                        thread_id=thread_id,
                        created_at=now,
                    )
                    session.add(row)
                row.workspace = str(data.get("workspace") or "")
                row.workspace_id = str(
                    data.get("workspace_id") or ""
                )[:36]
                row.workspace_kind = str(
                    data.get("workspace_kind") or ""
                )[:16]
                row.a2l_id = str(data.get("a2l_id") or "")[:64]
                row.root_handle = str(
                    data.get("root_handle") or ""
                )[:128]
                row.access_mode = str(
                    data.get("access_mode") or "read_only"
                )
                row.model = str(data.get("model") or "")
                row.provider = str(data.get("provider") or "")
                row.uploaded_bytes = int(data.get("uploaded_bytes") or 0)
                row.title = str(data.get("title") or "")
                if "pinned" in data:
                    row.pinned = bool(data.get("pinned"))
                if "archived" in data:
                    row.archived = bool(data.get("archived"))
                if "tags" in data:
                    row.tags = _tags_to_text(data.get("tags"))
                if "unread" in data:
                    row.unread = bool(data.get("unread"))
                if "last_message" in data:
                    row.last_message = str(
                        data.get("last_message") or ""
                    )[:160]
                row.deleted = False
                row.updated_at = now
            for thread_id in deleted:
                thread = str(thread_id or "")
                if not thread:
                    continue
                row = session.get(ControlSession, (str(tenant), thread))
                if row is None:
                    row = ControlSession(
                        tenant=str(tenant),
                        thread_id=thread,
                        workspace="",
                        created_at=now,
                    )
                    session.add(row)
                row.deleted = True
                row.updated_at = now

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

    def count_sessions(self, tenant: str) -> int:
        with Session(self.engine) as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(ControlSession)
                    .where(ControlSession.tenant == str(tenant))
                )
                or 0
            )

    def ping(self) -> bool:
        """轻量连通性检查；供健康端点与降级判断使用。"""
        try:
            with self.engine.connect() as session:
                session.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - 数据库故障是预期状态
            return False

    def close(self) -> None:
        self.engine.dispose()

    # ---------- 企业用量与配额 ----------

    def record_usage(
        self, tenant: str, user: str, records: list[dict[str, Any]]
    ) -> int:
        """把新增的 run_end 审计快照累加到用户日用量。

        幂等键是审计 record_hash。resume 产生的 run_end 是同一 run_epoch
        的累计值，按 cursor 取差值，不能直接相加。
        """
        tenant = str(tenant)
        user = str(user)
        applied = 0
        with Session(self.engine) as session, session.begin():
            for record in records:
                if str(record.get("kind") or "") not in {
                    "run_end",
                    "run_error",
                }:
                    continue
                record_hash = str(record.get("hash") or "")
                if not record_hash:
                    continue
                if session.get(ControlUsageEvent, (tenant, record_hash)) is not None:
                    continue
                epoch = str(record.get("run_epoch") or "")
                if not epoch:
                    # 旧版 A3 没有 epoch，无法区分 resume 累计值，跳过以免多记。
                    _LOG.debug("run_end 缺少 run_epoch，跳过计量")
                    continue
                thread_id = str(record.get("thread_id") or "")
                ts = str(record.get("ts") or "")
                day = ts[:10] if len(ts) >= 10 else _today()
                input_tokens = max(0, int(record.get("input_tokens") or 0))
                output_tokens = max(0, int(record.get("output_tokens") or 0))
                worker_in = max(
                    0, int(record.get("worker_input_tokens") or 0)
                )
                worker_out = max(
                    0, int(record.get("worker_output_tokens") or 0)
                )
                total = input_tokens + output_tokens + worker_in + worker_out
                cost = max(0.0, float(record.get("cost_usd") or 0))
                billed = max(0.0, float(record.get("billed_usd") or 0))

                cursor = session.get(
                    ControlUsageCursor, (tenant, user, thread_id)
                )
                same_epoch = (
                    cursor is not None and cursor.run_epoch == epoch
                )
                if same_epoch and cursor is not None:
                    delta_in = max(0, input_tokens - cursor.input_tokens)
                    delta_out = max(0, output_tokens - cursor.output_tokens)
                    delta_win = max(
                        0, worker_in - cursor.worker_input_tokens
                    )
                    delta_wout = max(
                        0, worker_out - cursor.worker_output_tokens
                    )
                    delta_cost = max(0.0, cost - cursor.cost_usd)
                    delta_billed = max(0.0, billed - cursor.billed_usd)
                else:
                    delta_in, delta_out = input_tokens, output_tokens
                    delta_win, delta_wout = worker_in, worker_out
                    delta_cost, delta_billed = cost, billed

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
                    daily = ControlUserUsageDaily(
                        tenant=tenant, user=user, day=day
                    )
                    session.add(daily)
                    session.flush()
                daily.input_tokens += delta_in
                daily.output_tokens += delta_out
                daily.worker_input_tokens += delta_win
                daily.worker_output_tokens += delta_wout
                daily.cost_usd += delta_cost
                daily.billed_usd += delta_billed
                if not same_epoch:
                    daily.runs += 1

                session.add(
                    ControlUsageEvent(
                        tenant=tenant,
                        record_hash=record_hash,
                        user=user,
                        thread_id=thread_id,
                        run_epoch=epoch,
                        ts=ts,
                        tokens=total,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        worker_input_tokens=worker_in,
                        worker_output_tokens=worker_out,
                        cost_usd=cost,
                        billed_usd=billed,
                    )
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
                applied += 1
        return applied

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
        """清理过期预留并归还对应额度；多实例各自执行也安全。"""
        cutoff = str(now or _now())
        with Session(self.engine) as session, session.begin():
            rows = session.scalars(
                select(ControlQuotaReservation)
                .where(ControlQuotaReservation.expires_at <= cutoff)
                .with_for_update()
            ).all()
            for row in rows:
                daily = session.scalar(
                    select(ControlUserUsageDaily)
                    .where(
                        ControlUserUsageDaily.tenant == row.tenant,
                        ControlUserUsageDaily.user == row.user,
                        ControlUserUsageDaily.day == row.day,
                    )
                    .with_for_update()
                )
                if daily is not None:
                    daily.reserved_tokens = max(
                        0, daily.reserved_tokens - row.tokens
                    )
                    daily.reserved_usd = max(
                        0.0, daily.reserved_usd - row.cost_usd
                    )
                session.delete(row)
            return len(rows)

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

        预留是独立行，带 owner/expires_at；进程崩溃后其他实例或下次启动
        只清理过期行，不会误伤仍在运行的其他实例。
        """
        tenant = str(tenant)
        user = str(user)
        # 先清理本用户当天的过期预留，再进入加锁事务。
        self.purge_expired_reservations()
        with Session(self.engine) as session, session.begin():
            quota = session.get(ControlUserQuota, (tenant, user))
            quota_data = _quota_row(quota)
            if not quota_data["enabled"]:
                return {"id": "", "tokens": 0, "cost_usd": 0.0}
            daily = session.scalar(
                select(ControlUserUsageDaily)
                .where(
                    ControlUserUsageDaily.tenant == tenant,
                    ControlUserUsageDaily.user == user,
                    ControlUserUsageDaily.day == str(day),
                )
                .with_for_update()
            )
            if daily is None:
                daily = ControlUserUsageDaily(
                    tenant=tenant, user=user, day=str(day)
                )
                session.add(daily)
                session.flush()
            used_tokens = (
                daily.input_tokens
                + daily.output_tokens
                + daily.worker_input_tokens
                + daily.worker_output_tokens
            )
            limit_tokens = int(quota_data["daily_tokens"] or 0)
            limit_cost_cny = Decimal(
                str(quota_data["daily_cost_cny"] or 0)
            )
            rate = Decimal(str(usd_to_cny or 1))
            used_cost_cny = (
                Decimal(str(daily.cost_usd or 0))
                + Decimal(str(daily.reserved_usd or 0))
            ) * rate
            reserve_tokens = max(0, int(tokens or 0))
            reserve_cost = max(0.0, float(cost_usd or 0))
            reserve_cost_cny = Decimal(str(reserve_cost)) * rate
            if limit_tokens > 0 and (
                used_tokens + daily.reserved_tokens + reserve_tokens
                > limit_tokens
            ):
                raise QuotaExceeded(
                    {
                        "kind": "tokens",
                        "limit": limit_tokens,
                        "used": used_tokens + daily.reserved_tokens,
                        "day": str(day),
                    }
                )
            if limit_cost_cny > 0 and (
                used_cost_cny + reserve_cost_cny > limit_cost_cny
            ):
                raise QuotaExceeded(
                    {
                        "kind": "cost_cny",
                        "limit": float(limit_cost_cny),
                        "used": float(used_cost_cny),
                        "day": str(day),
                    }
                )
            daily.reserved_tokens += reserve_tokens
            daily.reserved_usd += reserve_cost
            reservation = ControlQuotaReservation(
                id=uuid4().hex,
                tenant=tenant,
                user=user,
                day=str(day),
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
                "day": str(day),
            }

    def settle_quota(self, reservation_id: str) -> None:
        """按预留 id 释放；实际用量由审计累加，不在这里重复记账。"""
        rid = str(reservation_id or "")
        if not rid:
            return
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlQuotaReservation, rid)
            if row is None:
                return
            daily = session.scalar(
                select(ControlUserUsageDaily)
                .where(
                    ControlUserUsageDaily.tenant == row.tenant,
                    ControlUserUsageDaily.user == row.user,
                    ControlUserUsageDaily.day == row.day,
                )
                .with_for_update()
            )
            if daily is not None:
                daily.reserved_tokens = max(
                    0, daily.reserved_tokens - row.tokens
                )
                daily.reserved_usd = max(
                    0.0, daily.reserved_usd - row.cost_usd
                )
            session.delete(row)


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
