"""A2L 传输抽象。

生产传输是 A2L 主动外连的 TLS；本地测试用内存传输验证协议与生命周期。
mTLS 由调用方显式提供证书配置；未配置时保持旧的 loopback/TCP 行为。
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from ubismart.host_broker.protocol import MAX_FRAME_BYTES


class Transport(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str | None: ...

    async def close(self) -> None: ...

    def is_encrypted(self) -> bool: ...

    def peer_cert_fingerprint(self) -> str: ...


@dataclass(frozen=True, slots=True)
class TlsConfig:
    """A2L 作为 TLS 客户端时的配置。"""

    ca: str = ""
    cert: str = ""
    key: str = ""
    server_name: str = ""
    require_client_cert: bool = True
    check_hostname: bool = True


@dataclass(frozen=True, slots=True)
class ServerTlsConfig:
    """A3/A2 作为 TLS 服务端时的配置。"""

    cert: str
    key: str
    ca: str = ""
    require_client_cert: bool = True


def certificate_fingerprint(path: str) -> str:
    """返回 PEM 证书 DER 的 SHA-256（hex）；用于 a2l_id/设备绑定。"""
    pem = Path(path).read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def _client_context(tls: TlsConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cafile=tls.ca or None
    )
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = bool(tls.check_hostname)
    if tls.require_client_cert:
        if not tls.cert or not tls.key:
            raise ValueError("mTLS 需要客户端证书与私钥")
    if tls.cert or tls.key:
        if not tls.cert or not tls.key:
            raise ValueError("客户端证书与私钥必须同时提供")
        context.load_cert_chain(tls.cert, tls.key)
    return context


def _server_context(tls: ServerTlsConfig) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls.cert, tls.key)
    if tls.require_client_cert:
        if not tls.ca:
            raise ValueError("要求客户端证书时必须提供 CA")
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(tls.ca)
    else:
        context.verify_mode = ssl.CERT_NONE
    return context


class MemoryTransport:
    """单向管道：写入 `outbox`，从 `inbox` 读取；`None` 表示对端关闭。"""

    def __init__(
        self,
        inbox: asyncio.Queue[str | None],
        outbox: asyncio.Queue[str | None],
        *,
        peer_identity: dict[str, str] | None = None,
    ) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._closed = False
        self._peer_identity = dict(peer_identity or {})

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

    def is_encrypted(self) -> bool:
        return bool(self._peer_identity.get("encrypted"))

    def peer_cert_fingerprint(self) -> str:
        return str(self._peer_identity.get("fingerprint") or "")


def memory_transport_pair(
    *,
    client_peer: dict[str, str] | None = None,
    server_peer: dict[str, str] | None = None,
) -> tuple[MemoryTransport, MemoryTransport]:
    """返回 (client, server) 两个端点。

    `client_peer` 描述 client 看到的对端（A2L），`server_peer` 描述
    server 看到的对端（A3），用于协议级身份绑定测试。
    """
    to_server: asyncio.Queue[str | None] = asyncio.Queue()
    to_client: asyncio.Queue[str | None] = asyncio.Queue()
    client = MemoryTransport(
        inbox=to_client, outbox=to_server, peer_identity=client_peer
    )
    server = MemoryTransport(
        inbox=to_server, outbox=to_client, peer_identity=server_peer
    )
    return client, server


class TcpTransport:
    """loopback/内网 TCP 上的换行分隔 JSON 帧，可选 TLS。"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        ssl_object: ssl.SSLObject | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._ssl_object = ssl_object

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

    def is_encrypted(self) -> bool:
        return self._ssl_object is not None

    def peer_cert_fingerprint(self) -> str:
        if self._ssl_object is None:
            return ""
        try:
            der = self._ssl_object.getpeercert(binary_form=True)
        except Exception:
            return ""
        return hashlib.sha256(der).hexdigest() if der else ""


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


async def connect_tcp(
    url: str, *, tls: TlsConfig | None = None
) -> TcpTransport:
    host, port = parse_tcp_url(url)
    context = _client_context(tls) if tls is not None else None
    reader, writer = await asyncio.open_connection(
        host,
        port,
        limit=MAX_FRAME_BYTES + 1,
        ssl=context,
        server_hostname=(
            (tls.server_name or host) if context is not None else None
        ),
    )
    ssl_object = writer.get_extra_info("ssl_object") if context else None
    return TcpTransport(reader, writer, ssl_object=ssl_object)


async def start_tcp_server(
    handler: Callable[[TcpTransport], Awaitable[None]],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    tls: ServerTlsConfig | None = None,
) -> tuple[asyncio.Server, int]:
    context = _server_context(tls) if tls is not None else None

    async def _client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        transport = TcpTransport(
            reader,
            writer,
            ssl_object=writer.get_extra_info("ssl_object"),
        )
        try:
            await handler(transport)
        finally:
            await transport.close()

    server = await asyncio.start_server(
        _client,
        host,
        port,
        limit=MAX_FRAME_BYTES + 1,
        ssl=context,
    )
    sockets = server.sockets or []
    actual = int(sockets[0].getsockname()[1]) if sockets else port
    return server, actual
