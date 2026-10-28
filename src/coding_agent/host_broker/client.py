"""A4 侧的 A2L 客户端：只外连、握手、请求/响应、心跳与离线状态。

网络传输由 `transport.Transport` 注入；Phase 1 的测试用内存传输。
重连由客户端监督进程负责创建新实例，避免在未知副作用后盲目复用连接。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from coding_agent.host_broker.protocol import (
    GATEWAY_OFFLINE,
    PROTOCOL_VERSION,
    TIMEOUT,
    UNAUTHENTICATED,
    Frame,
    ProtocolError,
    decode_frame,
    encode_frame,
    make_cancel,
    make_grant_request,
    make_heartbeat,
    make_hello,
    make_request,
)
from coding_agent.host_broker.transport import Transport


class HostBrokerError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class HostBrokerOffline(HostBrokerError):
    def __init__(self, message: str = "A2L 不在线") -> None:
        super().__init__(GATEWAY_OFFLINE, message)


class HostBrokerClient:
    def __init__(
        self,
        transport: Transport,
        *,
        client_id: str,
        security_tier: str = "lowpriv",
        heartbeat_s: float = 15.0,
        request_timeout_s: float = 30.0,
        a2l_id: str = "",
        server_fingerprint: str = "",
        device_fingerprint: str = "",
        require_encrypted: bool = False,
    ) -> None:
        self.transport = transport
        self.client_id = str(client_id or "")
        self.security_tier = str(security_tier or "lowpriv")
        self.heartbeat_s = max(0.05, float(heartbeat_s))
        self.request_timeout_s = max(0.1, float(request_timeout_s))
        self.a2l_id = str(a2l_id or "")
        self.server_fingerprint = str(server_fingerprint or "")
        self.device_fingerprint = str(device_fingerprint or "")
        self.require_encrypted = bool(require_encrypted)
        self.state = "disconnected"
        self.hello: Frame | None = None
        self.hello_nonce = ""
        self.grant_events: asyncio.Queue[Frame] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[Frame]] = {}
        self._hello_future: asyncio.Future[Frame] | None = None
        self._hello_id = ""
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._last_seen = 0.0
        self._closing = False

    @property
    def is_online(self) -> bool:
        return self.state == "online"

    async def connect(self, *, timeout: float = 10.0) -> Frame:
        if self.state == "online" and self.hello is not None:
            return self.hello
        if self._closing:
            raise HostBrokerOffline("客户端已关闭")
        loop = asyncio.get_running_loop()
        self.state = "connecting"
        self._hello_future = loop.create_future()
        self._last_seen = time.monotonic()
        self._reader_task = asyncio.create_task(self._reader())
        nonce = secrets.token_hex(16)
        hello_frame = make_hello(
            self.client_id,
            security_tier=self.security_tier,
            a2l_id=self.a2l_id,
            nonce=nonce,
            server_fingerprint=self.server_fingerprint,
        )
        self._hello_id = hello_frame.id
        try:
            await self._send(hello_frame)
            hello = await asyncio.wait_for(self._hello_future, timeout=timeout)
            self._validate_hello(hello, nonce)
        except TimeoutError as exc:
            self.state = "offline"
            raise HostBrokerError(TIMEOUT, "A2L 握手超时") from exc
        except Exception:
            self.state = "offline"
            raise
        self.hello = hello
        self.hello_nonce = nonce
        self.state = "online"
        self._last_seen = time.monotonic()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return hello

    def _validate_hello(self, hello: Frame, nonce: str) -> None:
        meta = dict(hello.meta or {})
        if int(meta.get("protocol_version") or 0) != PROTOCOL_VERSION:
            raise HostBrokerError(UNAUTHENTICATED, "A2L 协议版本不匹配")
        if str(meta.get("nonce") or "") != nonce:
            raise HostBrokerError(UNAUTHENTICATED, "A2L 握手 nonce 不匹配")
        if self.a2l_id and str(meta.get("a2l_id") or "") != self.a2l_id:
            raise HostBrokerError(UNAUTHENTICATED, "A2L 身份不匹配")
        encrypted = bool(
            getattr(self.transport, "is_encrypted", lambda: False)()
        )
        if self.require_encrypted and not encrypted:
            raise HostBrokerError(UNAUTHENTICATED, "要求加密通道")
        peer_fp = str(
            getattr(self.transport, "peer_cert_fingerprint", lambda: "")()
        )
        claimed = str(meta.get("device_fingerprint") or "")
        if claimed and peer_fp and claimed != peer_fp:
            raise HostBrokerError(UNAUTHENTICATED, "A2L 设备证书指纹不匹配")
        if self.device_fingerprint and claimed != self.device_fingerprint:
            raise HostBrokerError(UNAUTHENTICATED, "A2L 设备指纹不符合预期")
        if self.require_encrypted and not claimed:
            raise HostBrokerError(UNAUTHENTICATED, "A2L 未提供设备指纹")
        kind = str(meta.get("attestation_kind") or "")
        public_key = str(meta.get("device_public_key") or "")
        signature = str(meta.get("attestation_signature") or "")
        if kind or public_key or signature:
            if not (kind and public_key and signature):
                raise HostBrokerError(
                    UNAUTHENTICATED, "A2L 设备证明字段不完整"
                )
            attestation_nonce = str(
                meta.get("attestation_nonce") or ""
            )
            if attestation_nonce and attestation_nonce != nonce:
                raise HostBrokerError(
                    UNAUTHENTICATED, "A2L 设备证明 nonce 不匹配"
                )
            from coding_agent.host_broker.attestation import (
                AttestationError,
                verify_evidence,
            )

            try:
                verify_evidence(
                    kind=kind,
                    public_key_b64=public_key,
                    message=f"{nonce}:{meta.get('a2l_id') or self.a2l_id}",
                    signature_b64=signature,
                )
            except AttestationError as exc:
                raise HostBrokerError(
                    UNAUTHENTICATED, f"A2L 设备证明无效：{exc}"
                ) from exc

    async def request(
        self,
        kind: str,
        *,
        workspace: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        grant_id: str = "",
        op_id: str = "",
        deadline_ms: int | None = None,
        timeout: float | None = None,
    ) -> Frame:
        if not self.is_online:
            raise HostBrokerOffline()
        frame = make_request(
            kind,
            op_id=op_id,
            workspace=workspace,
            payload=payload,
            grant_id=grant_id,
            deadline_ms=deadline_ms,
        )
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._pending[frame.id] = future
        try:
            await self._send(frame)
            return await asyncio.wait_for(
                future, timeout=timeout or self.request_timeout_s
            )
        except TimeoutError as exc:
            raise HostBrokerError(TIMEOUT, f"{kind} 超时") from exc
        finally:
            self._pending.pop(frame.id, None)

    async def request_grant(
        self,
        *,
        scope: list[str],
        verbs: list[str],
        ttl_s: int = 1800,
        reason: str = "",
        sensitive: bool = False,
        one_shot: bool = False,
        timeout: float | None = None,
    ) -> Frame:
        if not self.is_online:
            raise HostBrokerOffline()
        frame = make_grant_request(
            scope=scope,
            verbs=verbs,
            ttl_s=ttl_s,
            reason=reason,
            sensitive=sensitive,
            one_shot=one_shot,
        )
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._pending[frame.id] = future
        try:
            await self._send(frame)
            return await asyncio.wait_for(
                future,
                timeout=timeout or max(self.request_timeout_s, 300.0),
            )
        except TimeoutError as exc:
            raise HostBrokerError(TIMEOUT, "本地授权超时") from exc
        finally:
            self._pending.pop(frame.id, None)

    async def cancel(self, op_id: str) -> None:
        if self.is_online:
            await self._send(make_cancel(op_id))

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for task in (self._heartbeat_task, self._reader_task):
            if task is not None:
                task.cancel()
        for task in (self._heartbeat_task, self._reader_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._heartbeat_task = None
        self._reader_task = None
        try:
            await self.transport.close()
        except Exception:
            pass
        self.state = "stopped"
        self._fail_pending("客户端已关闭")

    async def _send(self, frame: Frame) -> None:
        try:
            await self.transport.send(encode_frame(frame))
        except Exception as exc:
            self.state = "offline"
            raise HostBrokerOffline(str(exc)) from exc

    async def _reader(self) -> None:
        try:
            while not self._closing:
                raw = await self.transport.recv()
                if raw is None:
                    break
                frame = decode_frame(raw)
                self._last_seen = time.monotonic()
                self._handle_frame(frame)
        except asyncio.CancelledError:
            return
        except ProtocolError:
            self.state = "offline"
            self._fail_pending("协议错误")
            return
        except Exception:
            self.state = "offline"
            self._fail_pending("A2L 连接异常")
            return
        if not self._closing:
            self.state = "offline"
            self._fail_pending("A2L 连接已断开")

    def _handle_frame(self, frame: Frame) -> None:
        if frame.id == self._hello_id and frame.type == "response":
            if self._hello_future is not None and not self._hello_future.done():
                if frame.error_code:
                    self._hello_future.set_exception(
                        HostBrokerError(frame.error_code, frame.error)
                    )
                else:
                    self._hello_future.set_result(frame)
            return
        if frame.type == "hello_ok":
            if self._hello_future is not None and not self._hello_future.done():
                self._hello_future.set_result(frame)
            return
        if frame.type in {"response", "grant.granted", "grant.denied"}:
            future = self._pending.get(frame.id)
            if future is not None and not future.done():
                future.set_result(frame)
                return
        if frame.type.startswith("grant."):
            self.grant_events.put_nowait(frame)

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closing and self.state == "online":
                await self._send(make_heartbeat())
                await asyncio.sleep(self.heartbeat_s)
                if time.monotonic() - self._last_seen > self.heartbeat_s * 3:
                    self.state = "offline"
                    self._fail_pending("心跳超时")
                    return
        except asyncio.CancelledError:
            return
        except HostBrokerError:
            self.state = "offline"
            self._fail_pending("心跳发送失败")

    def _fail_pending(self, message: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(HostBrokerOffline(message))
        self._pending.clear()
        if self._hello_future is not None and not self._hello_future.done():
            self._hello_future.set_exception(HostBrokerOffline(message))


class StaticTransport:
    """给离线/握手失败测试用的最小传输。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str | None:
        await asyncio.sleep(3600)
        return None

    async def close(self) -> None:
        return None

    def is_encrypted(self) -> bool:
        return False

    def peer_cert_fingerprint(self) -> str:
        return ""
