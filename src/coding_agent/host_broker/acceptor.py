"""A3/A4 侧的 A2L 接入端：接受客户端主动外连，持有 HostBrokerClient。

A2L 不监听；它连到 A3 的 loopback 端口后，本端用同一个 socket 发请求。
监听循环跑在独立线程里，工具层通过同步 `AcceptedHostCaller` 调用，
不把 asyncio 循环泄漏到主线程。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any

from coding_agent.host_broker.client import HostBrokerClient
from coding_agent.host_broker.devices import DeviceRegistry
from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.protocol import GATEWAY_OFFLINE
from coding_agent.host_broker.sync_client import GrantCache, call_with_grant
from coding_agent.host_broker.transport import (
    ServerTlsConfig,
    TcpTransport,
    connect_tcp,
    start_tcp_server,
)


class AcceptedHostCaller:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: HostBrokerClient,
        *,
        client_id: str,
        request_timeout_s: float = 30.0,
    ) -> None:
        self.loop = loop
        self.client = client
        self.client_id = str(client_id)
        self.request_timeout_s = float(request_timeout_s)
        self._grants = GrantCache()

    def _run(self, coro, *, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout or self.request_timeout_s + 5)

    def call(
        self,
        kind: str,
        *,
        workspace: dict[str, Any],
        payload: dict[str, Any],
        grant_id: str = "",
    ) -> dict[str, Any]:
        if not self.client.is_online:
            raise BrokerOpError(GATEWAY_OFFLINE, "A2L 不在线")
        return call_with_grant(
            self._run,
            self.client,
            kind=kind,
            workspace=workspace,
            payload=payload,
            grant_id=grant_id,
            client_id=self.client_id,
            request_timeout_s=self.request_timeout_s,
            grants=self._grants,
        )

    def close(self) -> None:
        # 连接断开时清掉缓存，重连后必须重新走本地授权。
        self._grants.clear_all()


class HostBrokerAcceptor:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        client_id: str = "a3",
        security_tier: str = "lowpriv",
        heartbeat_s: float = 15.0,
        request_timeout_s: float = 30.0,
        tls: ServerTlsConfig | None = None,
        require_encrypted: bool = False,
        a2l_id: str = "",
        server_fingerprint: str = "",
        device_fingerprint: str = "",
        device_registry: DeviceRegistry | None = None,
        device_reload_s: float = 2.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.client_id = client_id
        self.security_tier = security_tier
        self.heartbeat_s = float(heartbeat_s)
        self.request_timeout_s = float(request_timeout_s)
        self.tls = tls
        self.require_encrypted = bool(require_encrypted)
        self.a2l_id = str(a2l_id or "")
        self.server_fingerprint = str(server_fingerprint or "")
        self.device_fingerprint = str(device_fingerprint or "")
        self.device_registry = device_registry
        self.device_reload_s = max(0.5, float(device_reload_s))
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="a2l-acceptor",
            daemon=True,
        )
        self._ready = threading.Event()
        self._server: asyncio.Server | None = None
        self._clients: dict[str, HostBrokerClient] = {}
        # 每个 A2L 连接复用一个 caller，grant 缓存才能跨工具调用生效。
        self._callers: dict[str, AcceptedHostCaller] = {}
        self._client_ids: dict[str, tuple[str, str]] = {}
        self._watch_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> int:
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("A2L acceptor 启动超时")
        return self.port

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start())
        self.loop.run_forever()

    async def _start(self) -> None:
        self._server, self.port = await start_tcp_server(
            self._on_connection,
            host=self.host,
            port=self.port,
            tls=self.tls,
        )
        if self.device_registry is not None:
            self._watch_task = asyncio.create_task(self._watch_devices())
        self._ready.set()

    def _authorize_device(self, client: HostBrokerClient, hello) -> str:
        """返回空串表示通过；否则返回拒绝原因。"""
        if self.device_registry is None:
            return ""
        meta = dict(hello.meta or {})
        a2l_id = str(meta.get("a2l_id") or "")
        fingerprint = ""
        if client.transport is not None:
            fingerprint = client.transport.peer_cert_fingerprint()
        fingerprint = fingerprint or str(meta.get("device_fingerprint") or "")
        decision = self.device_registry.authorize(a2l_id, fingerprint)
        if not decision.allowed:
            return f"{a2l_id}: {decision.reason}"
        record = decision.record
        if record is not None and record.public_key:
            evidence = {
                "kind": str(meta.get("attestation_kind") or ""),
                "signature": str(
                    meta.get("attestation_signature") or ""
                ),
            }
            attested = self.device_registry.verify_attestation(
                record,
                a2l_id=a2l_id,
                nonce=client.hello_nonce,
                evidence=evidence,
            )
            if not attested.allowed:
                return f"{a2l_id}: {attested.reason}"
        return ""

    async def _watch_devices(self) -> None:
        assert self.device_registry is not None
        while not self._closed:
            await asyncio.sleep(self.device_reload_s)
            self.device_registry.reload_if_changed()
            for broker_id, client in list(self._clients.items()):
                a2l_id, fingerprint = self._client_ids.get(
                    broker_id, ("", "")
                )
                decision = self.device_registry.authorize(
                    a2l_id, fingerprint
                )
                if decision.allowed:
                    continue
                print(
                    f"A2L 设备已失效，断开 {broker_id}：{decision.reason}",
                    file=sys.stderr,
                    flush=True,
                )
                await self._drop_client(broker_id, client)

    async def _drop_client(
        self, broker_id: str, client: HostBrokerClient
    ) -> None:
        self._clients.pop(broker_id, None)
        self._client_ids.pop(broker_id, None)
        caller = self._callers.pop(broker_id, None)
        if caller is not None:
            caller.close()
        await client.close()

    async def _on_connection(self, transport: TcpTransport) -> None:
        client = HostBrokerClient(
            transport,
            client_id=self.client_id,
            security_tier=self.security_tier,
            heartbeat_s=self.heartbeat_s,
            request_timeout_s=self.request_timeout_s,
            # 有设备台账时允许多个 a2l_id，由台账逐台校验。
            a2l_id=(
                "" if self.device_registry is not None else self.a2l_id
            ),
            server_fingerprint=self.server_fingerprint,
            device_fingerprint=self.device_fingerprint,
            require_encrypted=self.require_encrypted,
        )
        try:
            hello = await client.connect()
            reason = self._authorize_device(client, hello)
            if reason:
                print(
                    f"A2L 设备未通过校验：{reason}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            broker_id = str(
                (hello.meta or {}).get("broker_id") or client.client_id
            )
            meta = dict(hello.meta or {})
            fingerprint = transport.peer_cert_fingerprint() or str(
                meta.get("device_fingerprint") or ""
            )
            print(
                f"A2L connected: {broker_id}",
                file=sys.stderr,
                flush=True,
            )
            old = self._clients.get(broker_id)
            self._clients[broker_id] = client
            self._client_ids[broker_id] = (
                str(meta.get("a2l_id") or ""),
                fingerprint,
            )
            if old is not None:
                await old.close()
            while not self._closed and client.is_online:
                await asyncio.sleep(0.05)
        finally:
            for key, value in list(self._clients.items()):
                if value is client:
                    self._clients.pop(key, None)
                    self._client_ids.pop(key, None)
            for key, caller in list(self._callers.items()):
                if caller.client is client:
                    self._callers.pop(key, None)
                    caller.close()
            await client.close()

    def clients(self) -> list[str]:
        return sorted(self._clients)

    def caller_for(self, broker_id: str) -> AcceptedHostCaller | None:
        key = str(broker_id or "")
        client = self._clients.get(key)
        if client is None:
            self._callers.pop(key, None)
            return None
        caller = self._callers.get(key)
        if caller is None or caller.client is not client:
            caller = AcceptedHostCaller(
                self.loop,
                client,
                client_id=self.client_id,
                request_timeout_s=self.request_timeout_s,
            )
            self._callers[key] = caller
        return caller

    def stop(self) -> None:
        self._closed = True

        async def _stop() -> None:
            if self._watch_task is not None:
                self._watch_task.cancel()
                try:
                    await self._watch_task
                except (asyncio.CancelledError, Exception):
                    pass
            for client in list(self._clients.values()):
                await client.close()
            self._clients.clear()
            self._client_ids.clear()
            self._callers.clear()
            # 给 _on_connection 一个检查 closed / offline 的机会，避免在
            # transport 处理任务仍 pending 时停掉事件循环。
            await asyncio.sleep(0.1)
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(_stop(), self.loop).result(5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(5)
            self.loop.close()


async def connect_a2l_outbound(url: str) -> TcpTransport:
    """给 A2L 进程用：主动连到 A3 的监听端。"""
    return await connect_tcp(url)
