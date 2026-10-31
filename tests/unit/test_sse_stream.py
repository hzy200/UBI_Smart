"""SSE 心跳与响应头。"""

from __future__ import annotations

import asyncio

import pytest

from ubismart.protocol.sse_stream import (
    sse_headers,
    with_heartbeat,
    with_heartbeat_bytes,
)


def test_sse_headers_disable_buffering_and_transforms() -> None:
    headers = sse_headers("t1")
    assert headers["Cache-Control"] == "no-cache, no-transform"
    assert headers["X-Accel-Buffering"] == "no"
    assert headers["Connection"] == "keep-alive"
    assert headers["X-Thread-Id"] == "t1"
    assert "X-Thread-Id" not in sse_headers()


def test_heartbeat_is_emitted_during_silence() -> None:
    async def source():
        yield "data: one\n\n"
        await asyncio.sleep(0.05)
        yield "data: two\n\n"

    async def collect() -> list[str]:
        return [
            item
            async for item in with_heartbeat(source(), interval_s=0.01)
        ]

    items = asyncio.run(collect())
    assert items[0] == "data: one\n\n"
    assert items[-1] == "data: two\n\n"
    assert ": keepalive\n\n" in items


def test_byte_heartbeat_for_proxied_streams() -> None:
    async def source():
        yield b"data: one\n\n"
        await asyncio.sleep(0.05)

    async def collect() -> list[bytes]:
        return [
            item
            async for item in with_heartbeat_bytes(
                source(), interval_s=0.01
            )
        ]

    items = asyncio.run(collect())
    assert items[0] == b"data: one\n\n"
    assert b": keepalive\n\n" in items


def test_heartbeat_propagates_source_errors() -> None:
    async def source():
        yield "data: one\n\n"
        raise RuntimeError("boom")

    async def collect() -> None:
        async for _ in with_heartbeat(source(), interval_s=0.01):
            pass

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(collect())
