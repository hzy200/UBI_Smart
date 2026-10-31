"""SSE 心跳与响应头。

心跳用注释帧（`: keepalive`），前端解析器会忽略；它只用于让反代/浏览器
看到连接仍有数据，避免 60s 空闲超时。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import TypeVar

SSE_HEARTBEAT_INTERVAL_S = 15.0
SSE_HEARTBEAT_MS = int(SSE_HEARTBEAT_INTERVAL_S * 1000)

T = TypeVar("T", str, bytes)
_DONE = object()


def sse_headers(thread_id: str = "") -> dict[str, str]:
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    if thread_id:
        headers["X-Thread-Id"] = thread_id
    return headers


async def _pump(source: AsyncIterator[T], queue: asyncio.Queue) -> None:
    try:
        async for item in source:
            await queue.put((False, item))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 交给消费者继续抛
        await queue.put((True, exc))
    finally:
        await queue.put((False, _DONE))


async def _with_heartbeat(
    source: AsyncIterator[T],
    heartbeat: T,
    interval_s: float,
) -> AsyncIterator[T]:
    queue: asyncio.Queue = asyncio.Queue(maxsize=1_000)
    task = asyncio.create_task(_pump(source, queue))
    started = False
    try:
        while True:
            try:
                # 首帧之前不发心跳：Windows/慢启动下 wait_for 可能先超时，
                # 让客户端把 keepalive 误当成流的第一条事件。
                if started:
                    failed, item = await asyncio.wait_for(
                        queue.get(), timeout=interval_s
                    )
                else:
                    failed, item = await queue.get()
            except TimeoutError:
                yield heartbeat
                continue
            if failed:
                raise item
            if item is _DONE:
                return
            started = True
            yield item
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


def with_heartbeat(
    source: AsyncIterator[str],
    *,
    interval_s: float = SSE_HEARTBEAT_INTERVAL_S,
) -> AsyncIterator[str]:
    return _with_heartbeat(source, ": keepalive\n\n", interval_s)


def with_heartbeat_bytes(
    source: AsyncIterator[bytes],
    *,
    interval_s: float = SSE_HEARTBEAT_INTERVAL_S,
) -> AsyncIterator[bytes]:
    return _with_heartbeat(source, b": keepalive\n\n", interval_s)
