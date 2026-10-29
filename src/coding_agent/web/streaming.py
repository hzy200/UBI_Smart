"""运行事件流管理：缓冲、订阅、跨进程重放与收尾。

从 `web/app.py::create_app` 抽出。管理器只持有流状态，运行实例的创建/销毁
由调用方以回调注入，避免把 A2 的装配细节带进来。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from coding_agent.control.webhooks import schedule_webhooks
from coding_agent.events import ApprovalRequested, Event, RunFailed, RunFinished
from coding_agent.platform.observability import note_silent_failure
from coding_agent.protocol.sse_stream import sse_headers, with_heartbeat
from coding_agent.web.local_runtime import RuntimeEntry
from coding_agent.web.push import schedule_push
from coding_agent.web.routes.notifications import notification_payload
from coding_agent.web.sse import (
    StreamEntry,
    event_is_terminal,
    event_key,
    events_after,
    is_suspended,
    sse,
    sse_with_id,
)

# 每个会话的重放缓冲上限；超出后丢最旧事件（序号仍连续）。
STREAM_BUFFER_MAX_CHARS = 8 * 1024 * 1024


class StreamManager:
    def __init__(
        self,
        *,
        transport: Any,
        control_store: Any,
        push_service: Any,
        webhook_service: Any,
        lookup_runtime: Callable[[str, str], RuntimeEntry | None],
        drop_runtime: Callable[[tuple[str, str], Any], Awaitable[None]],
    ) -> None:
        self.transport = transport
        self.control_store = control_store
        self.push_service = push_service
        self.webhook_service = webhook_service
        self.lookup_runtime = lookup_runtime
        self.drop_runtime = drop_runtime
        self.streams: dict[tuple[str, str], StreamEntry] = {}
        self.registry_lock = threading.Lock()

    def append_event(self, entry: StreamEntry, event: Event) -> None:
        entry.seq += 1
        entry.events.append((entry.seq, event))
        entry.chars += len(event.model_dump_json())
        while entry.chars > STREAM_BUFFER_MAX_CHARS and len(entry.events) > 1:
            _, old = entry.events.pop(0)
            entry.chars -= len(old.model_dump_json())
        if len(entry.events) > 5_000:
            dropped = entry.events[: len(entry.events) - 5_000]
            del entry.events[: len(entry.events) - 5_000]
            for _, old in dropped:
                entry.chars -= len(old.model_dump_json())
        entry.updated = time.monotonic()
        if self.transport is not None:
            try:
                self.transport.publish(
                    event_key(entry.key[0], entry.public_thread),
                    entry.seq,
                    event.model_dump_json(),
                )
            except Exception:  # noqa: BLE001 - 传输故障不能中断运行
                _LOG.warning("事件传输发布失败", exc_info=True)
        for queue in list(entry.subscribers):
            try:
                queue.put_nowait((entry.seq, event))
            except asyncio.QueueFull:
                pass

    async def record_event_notification(
        self, user_name: str, public_thread: str, event: Event
    ) -> None:
        """本地 runtime 路径也写站内通知；代理路径由 runs.py 负责。"""
        if self.control_store is None:
            return
        if not isinstance(event, (ApprovalRequested, RunFailed, RunFinished)):
            return
        notice = notification_payload(event.model_dump())
        if notice is None:
            return
        kind, title, body, dedupe = notice
        try:
            row = await asyncio.to_thread(
                self.control_store.add_notification,
                user_name,
                kind=kind,
                title=title,
                body=body,
                thread_id=public_thread,
                dedupe_key=dedupe,
            )
            if row:
                schedule_push(self.push_service, user_name, row)
                schedule_webhooks(
                    self.webhook_service,
                    user_name,
                    str(getattr(event, "type", "") or ""),
                    {
                        "title": title,
                        "body": body,
                        "kind": kind,
                        "thread_id": public_thread,
                        "created_at": row.get("created_at", ""),
                    },
                )
        except Exception as exc:  # noqa: BLE001 - 通知失败不打断运行
            note_silent_failure("notification")
            _LOG.warning("通知写入失败：%s", exc)

    async def _consume(
        self, entry: StreamEntry, events: AsyncIterator[Event]
    ) -> None:
        try:
            async for event in events:
                if is_suspended(event):
                    entry.suspended = True
                self.append_event(entry, event)
                await self.record_event_notification(
                    entry.key[0], entry.public_thread, event
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 错误以事件形式送达订阅者
            failed = RunFailed(message=f"{type(exc).__name__}: {exc}")
            self.append_event(entry, failed)
        finally:
            if not entry.suspended:
                await self.drop_runtime(entry.key, entry.runtime)
            entry.done = True
            entry.updated = time.monotonic()
            for queue in list(entry.subscribers):
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def guard(
        self, key: tuple[str, str], *, require_suspended: bool = False
    ) -> None:
        """拒绝在 active 流上再启动 resume；跨进程 resume 没有流时放行。"""
        old = self.streams.get(key)
        if old is None:
            return
        if not old.done:
            raise HTTPException(
                status_code=409,
                detail="该会话已有运行中的事件流；请等待或先取消",
            )
        if require_suspended and not old.suspended:
            raise HTTPException(
                status_code=409,
                detail="该会话没有待审批的挂起",
            )

    def start(
        self,
        key: tuple[str, str],
        public_thread: str,
        runtime: Any,
        events: AsyncIterator[Event],
    ) -> StreamEntry:
        old = self.streams.pop(key, None)
        # 序号跨 resume/进程重启保持单调；传输层可能保留旧进程的事件。
        base_seq = old.seq if old is not None else 0
        if self.transport is not None:
            try:
                base_seq = max(
                    base_seq,
                    self.transport.latest_seq(event_key(key[0], public_thread)),
                )
            except Exception:  # noqa: BLE001
                _LOG.warning("读取事件传输序号失败", exc_info=True)
        # 每个用户只保留最近 8 个已完成流，避免长跑服务被重放缓冲撑爆。
        with self.registry_lock:
            owned = [
                (other_key, other)
                for other_key, other in self.streams.items()
                if other_key[0] == key[0] and other.done
            ]
            if len(owned) >= 8:
                owned.sort(key=lambda item: item[1].updated)
                for other_key, _ in owned[: len(owned) - 7]:
                    self.streams.pop(other_key, None)
        entry = StreamEntry(
            key=key,
            public_thread=public_thread,
            runtime=runtime,
            seq=base_seq,
        )
        entry.task = asyncio.create_task(self._consume(entry, events))
        self.streams[key] = entry
        return entry

    def buffered_sse(
        self, entry: StreamEntry, *, after: int = 0
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last = max(0, int(after))
            queue: asyncio.Queue = asyncio.Queue(maxsize=2_000)
            entry.subscribers.append(queue)
            try:
                while True:
                    for seq, event in events_after(entry, last):
                        last = seq
                        yield sse_with_id(event, seq)
                    if entry.done and last >= entry.seq:
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        # 先把 done 前最后一批事件发完，再退出。
                        for seq, event in events_after(entry, last):
                            last = seq
                            yield sse_with_id(event, seq)
                        break
                    seq, event = item
                    if seq <= last:
                        continue
                    last = seq
                    yield sse_with_id(event, seq)
            finally:
                try:
                    entry.subscribers.remove(queue)
                except ValueError:
                    pass

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers=sse_headers(entry.public_thread),
        )

    def transport_sse(
        self, event_key_value: str, public_thread: str, *, after: int = 0
    ) -> StreamingResponse:
        """没有本地 StreamEntry 时，从共享传输按 seq 重放并继续轮询。"""

        async def stream() -> AsyncIterator[str]:
            last = max(0, int(after))
            idle = 0.0
            silent = 0.0
            while self.transport is not None:
                try:
                    rows = await asyncio.to_thread(
                        self.transport.replay, event_key_value, last
                    )
                except Exception:  # noqa: BLE001 - 传输故障不伪造事件
                    _LOG.warning("事件传输重放失败", exc_info=True)
                    return
                for seq, payload in rows:
                    if seq <= last:
                        continue
                    last = seq
                    idle = silent = 0.0
                    yield f"id: {seq}\ndata: {payload}\n\n"
                    if event_is_terminal(payload):
                        return
                await asyncio.sleep(0.5)
                idle += 0.5
                silent += 0.5
                if idle >= 15:
                    yield ": keepalive\n\n"
                    idle = 0.0
                if silent >= 300:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers=sse_headers(public_thread),
        )

    def get_entry(self, user: str, public_thread: str) -> RuntimeEntry | None:
        entry = self.lookup_runtime(user, public_thread)
        if entry is not None:
            entry.last_active = time.monotonic()
        return entry

    def sse_response(
        self,
        key: tuple[str, str],
        public_thread: str,
        runtime: Any,
        events: AsyncIterator[Event],
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            suspended = False
            try:
                async for event in events:
                    if is_suspended(event):
                        suspended = True
                    yield sse(event)
                    await self.record_event_notification(
                        key[0], public_thread, event
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 错误以事件形式送到前端
                yield sse(RunFailed(message=f"{type(exc).__name__}: {exc}"))
            finally:
                if suspended:
                    entry = self.get_entry(*key)
                    if entry is not None:
                        entry.last_active = time.monotonic()
                else:
                    await self.drop_runtime(key, runtime)

        return StreamingResponse(
            with_heartbeat(stream()),
            media_type="text/event-stream",
            headers=sse_headers(public_thread),
        )


_LOG = logging.getLogger("coding_agent.web.streaming")
