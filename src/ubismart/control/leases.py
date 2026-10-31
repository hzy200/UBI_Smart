"""会话级运行租约（RFC-0001，M4）。

A2 在转发 run/resume 前抢占，A2 崩溃后由 TTL 自动失效。
A3 不连接 MySQL，仍保留自己的进程内活跃判断；两层是互补关系。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ubismart.control.schema import RunLease

_LOG = logging.getLogger(__name__)


class LeaseHeld(RuntimeError):
    """会话已被其他租约持有。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


class RunLeaseStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def acquire(
        self,
        session_key: str,
        owner: str,
        *,
        ttl_s: int = 120,
        lease_id: str = "",
    ) -> str:
        key = str(session_key or "").strip()
        if not key:
            raise ValueError("session_key 不能为空")
        lease = str(lease_id or uuid.uuid4())
        now = _now()
        now_text = _fmt(now)
        expires = _fmt(now + timedelta(seconds=max(1, int(ttl_s))))
        # 不用 `SELECT ... FOR UPDATE`：SQLite 会**静默忽略**行锁，读-改-写在
        # 两个后端上都不是原子的（两个实例能同时读到"没人持有"再同时写入）。
        # 判定与写入必须落在同一条语句里。
        steal = (
            update(RunLease)
            .where(
                RunLease.session_key == key,
                or_(
                    RunLease.expires_at <= now_text,
                    RunLease.lease_id == lease,
                ),
            )
            .values(
                lease_id=lease,
                owner=str(owner or ""),
                expires_at=expires,
            )
        )
        try:
            with Session(self.engine) as session, session.begin():
                if session.execute(steal).rowcount:
                    return lease
                # 没抢到：要么正被别人持有，要么这一行还不存在。
                row = session.get(RunLease, key)
                if row is not None:
                    raise LeaseHeld(
                        f"会话 {key} 已被租约 {row.lease_id} 持有，"
                        f"到期 {row.expires_at}"
                    )
                session.add(
                    RunLease(
                        session_key=key,
                        lease_id=lease,
                        owner=str(owner or ""),
                        expires_at=expires,
                        created_at=now_text,
                    )
                )
        except IntegrityError as exc:
            # 并发插入撞主键：fail closed —— 当作刚被别的实例抢占。
            raise LeaseHeld(f"会话 {key} 刚被其他实例抢占") from exc
        return lease

    def renew(self, session_key: str, lease_id: str, *, ttl_s: int = 120) -> bool:
        now = _now()
        statement = (
            update(RunLease)
            .where(
                RunLease.session_key == str(session_key),
                RunLease.lease_id == str(lease_id),
            )
            .values(
                expires_at=_fmt(
                    now + timedelta(seconds=max(1, int(ttl_s)))
                )
            )
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            return bool(result.rowcount)

    def release(self, session_key: str, lease_id: str) -> bool:
        statement = delete(RunLease).where(
            RunLease.session_key == str(session_key),
            RunLease.lease_id == str(lease_id),
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            return bool(result.rowcount)

    def force_release(self, session_key: str) -> bool:
        statement = delete(RunLease).where(
            RunLease.session_key == str(session_key)
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            return bool(result.rowcount)

    def active(self, session_key: str) -> dict | None:
        with Session(self.engine) as session:
            row = session.get(RunLease, str(session_key))
            if row is None:
                return None
            if row.expires_at <= _fmt(_now()):
                return None
            return {
                "session_key": row.session_key,
                "lease_id": row.lease_id,
                "owner": row.owner,
                "expires_at": row.expires_at,
                "created_at": row.created_at,
            }

    def purge_expired(self) -> int:
        statement = delete(RunLease).where(
            RunLease.expires_at <= _fmt(_now())
        )
        with Session(self.engine) as session, session.begin():
            result = session.execute(statement)
            return int(result.rowcount or 0)

    def list_active(self, owner: str = "") -> list[dict]:
        with Session(self.engine) as session:
            statement = select(RunLease).where(
                RunLease.expires_at > _fmt(_now())
            )
            if owner:
                statement = statement.where(RunLease.owner == owner)
            return [
                {
                    "session_key": row.session_key,
                    "lease_id": row.lease_id,
                    "owner": row.owner,
                    "expires_at": row.expires_at,
                }
                for row in session.scalars(statement).all()
            ]


class LeaseHeartbeat:
    """独立续租心跳：不依赖 SSE 事件，长静默工具也不会让租约过期。

    `stop_now()` 是同步的，供流结束回调使用；`stop()` 会等待任务退出。
    续租返回 False 说明租约已被其他实例抢走，只记录并停止；真正的
    双写防护仍由 A3 的进程内运行判断与工作区锁承担。
    """

    def __init__(
        self,
        store: RunLeaseStore,
        session_key: str,
        lease_id: str,
        *,
        ttl_s: int = 120,
        interval_s: float | None = None,
    ) -> None:
        self.store = store
        self.session_key = session_key
        self.lease_id = lease_id
        self.ttl_s = max(1, int(ttl_s))
        self.interval_s = float(
            interval_s
            if interval_s is not None
            else max(1.0, self.ttl_s / 3)
        )
        self._task: asyncio.Task | None = None
        self.lost = False

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                renewed = await asyncio.to_thread(
                    self.store.renew,
                    self.session_key,
                    self.lease_id,
                    ttl_s=self.ttl_s,
                )
            except Exception:  # noqa: BLE001 - 续租失败交给 TTL
                _LOG.warning("租约续期异常", exc_info=True)
                continue
            if not renewed:
                self.lost = True
                _LOG.warning(
                    "租约已被抢占：session=%s lease=%s",
                    self.session_key,
                    self.lease_id,
                )
                return

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop_now(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()

    async def stop(self) -> None:
        task = self._task
        self.stop_now()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
