"""A2L 传输抽象。

生产传输是客户端主动外连的 WebSocket/TLS；Phase 1 的单元测试用内存
传输验证协议与生命周期，不引入网络依赖。新增网络传输只需实现
`send/recv/close` 三个方法。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import urlparse

from coding_agent.host_broker.protocol import MAX_FRAME_BYTES


class Transport(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | None: ...

    async def close(self) -> None: ...


class MemoryTransport:
    """单向管道：写入 `outbox`，从 `inbox` 读取；`None` 表示对端关闭。"""

    def __init__(
        self,
        inbox: asyncio.Queue[str | None],
        outbox: asyncio.Queue[str | None],
    ) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._closed = False

    async def send(self, data: str) -> None:
        if self._closed:
            raise ConnectionError("transport closed")
        await self._outbox.put(data)

    async def recv(self) -> str | None:
        return await self._inbox.get()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            # 唤醒本地 recv 与对端 recv；单边 close 也必须能停止本端循环。
            await self._inbox.put(None)
            await self._outbox.put(None)


def memory_transport_pair() -> tuple[MemoryTransport, MemoryTransport]:
    """返回 (client, server) 两个端点。"""
    to_server: asyncio.Queue[str | None] = asyncio.Queue()
    to_client: asyncio.Queue[str | None] = asyncio.Queue()
    client = MemoryTransport(inbox=to_client, outbox=to_server)
    server = MemoryTransport(inbox=to_server, outbox=to_client)
    return client, server


class TcpTransport:
    """loopback/内网 TCP 上的换行分隔 JSON 帧。"""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, data: str) -> None:
        self._writer.write(data.encode("utf-8") + b"\n")
        await self._writer.drain()

    async def recv(self) -> str | None:
        raw = await self._reader.readline()
        if not raw:
            return None
        return raw.rstrip(b"\r\n").decode("utf-8")

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass


def parse_tcp_url(url: str) -> tuple[str, int]:
    text = str(url or "").strip()
    if "://" not in text:
        text = f"tcp://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in ("tcp", "ws", "wss"):
        raise ValueError(f"不支持的传输协议：{parsed.scheme}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    if port is None:
        raise ValueError("URL 缺少端口")
    return host, int(port)


async def connect_tcp(url: str) -> TcpTransport:
    host, port = parse_tcp_url(url)
    reader, writer = await asyncio.open_connection(
        host, port, limit=MAX_FRAME_BYTES + 1
    )
    return TcpTransport(reader, writer)


async def start_tcp_server(
    handler: Callable[[TcpTransport], Awaitable[None]],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[asyncio.Server, int]:
    async def _client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        transport = TcpTransport(reader, writer)
        try:
            await handler(transport)
        finally:
            await transport.close()

    server = await asyncio.start_server(
        _client, host, port, limit=MAX_FRAME_BYTES + 1
    )
    sockets = server.sockets or []
    actual = int(sockets[0].getsockname()[1]) if sockets else port
    return server, actual
