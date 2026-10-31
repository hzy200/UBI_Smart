"""A2L 服务端骨架：握手、请求分发、取消、暂停与审计。

传输与权限判定由外部注入；本模块只负责协议状态机，不直接读写宿主文件。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ubismart.host_broker.consent import ConsentProvider
from ubismart.host_broker.errors import BrokerOpError
from ubismart.host_broker.op_journal import (
    OP_CANCELLED,
    OP_FAILED,
    OP_FINISHED,
    OpJournal,
)
from ubismart.host_broker.policy import GrantStore
from ubismart.host_broker.protocol import (
    A2L_PAUSED,
    CANCELLED,
    GRANT_LOCAL_ONLY,
    GRANT_RATE_LIMITED,
    GRANT_USED,
    INTERNAL,
    OP_NOT_ENABLED_IN_PHASE1,
    OP_UNCERTAIN,
    OP_UNKNOWN,
    PROTOCOL_VERSION,
    UNAUTHENTICATED,
    Frame,
    ProtocolError,
    decode_frame,
    encode_frame,
    make_error,
    make_grant_denied,
    make_grant_granted,
    make_grant_pending,
    make_heartbeat,
    make_hello_ok,
    make_response,
    with_ids,
)
from ubismart.host_broker.transport import Transport

PHASE1_REQUEST_OPS = frozenset(
    {
        "host.health",
        "host.workspace.register",
        "host.list_dir",
        "host.find_files",
        "host.stat",
        "host.read_text",
        "host.read_chunk",
        "host.toolchains",
        "host.env.inspect",
        "host.snapshot_list",
    }
)

WRITE_REQUEST_OPS = frozenset(
    {
        "host.write_begin",
        "host.write_chunk",
        "host.write_commit",
        "host.write_abort",
        "host.write_text",
        "host.write_many_commit",
        "host.delete",
        "host.mkdir",
        "host.snapshot_save",
        "host.restore",
    }
)

EXEC_REQUEST_OPS = frozenset({"host.exec"})
INSTALL_REQUEST_OPS = frozenset({"host.install"})

# 写/执行/安装：同一 op_id 不能盲目重放。
NON_IDEMPOTENT_OPS = WRITE_REQUEST_OPS | EXEC_REQUEST_OPS | INSTALL_REQUEST_OPS

# 这些错误发生在真正写入/执行之前；日志行可以丢弃，允许换 grant 重试。
PRE_EXECUTION_DENIALS = frozenset(
    {
        "DENIED_ZX",
        "DENIED_SCOPE",
        "DENIED_VERB",
        "GRANT_REQUIRED",
        "GRANT_EXPIRED",
        "GRANT_REVOKED",
        "SENSITIVE_ROOT",
        "PATH_ESCAPE",
        "INVALID_PATH",
        "CONFLICT",
        "TOOLCHAIN_UNAVAILABLE",
        "EXEC_UNAVAILABLE",
    }
)

GRANT_FRAME_TYPES = frozenset(
    {
        "grant.request",
        "grant.pending",
        "grant.granted",
        "grant.denied",
        "grant.revoke",
    }
)

Handler = Callable[[Frame], Awaitable[Frame]]


HandlerError = BrokerOpError


class HostBrokerServer:
    def __init__(
        self,
        transport: Transport,
        *,
        broker_id: str,
        security_tier: str = "lowpriv",
        threat_model: str = "T1",
        capabilities: dict[str, Any] | None = None,
        handler: Handler | None = None,
        journal: OpJournal | None = None,
        lifecycle: Any | None = None,
        grants: GrantStore | None = None,
        consent: ConsentProvider | None = None,
        device_fingerprint: str = "",
        require_encrypted: bool = False,
        hello_nonce_ttl_s: float = 300.0,
        grant_rate_limit_s: float = 5.0,
        max_pending_grants: int = 1,
        allowed_ops: frozenset[str] | None = None,
        device_identity: Any | None = None,
    ) -> None:
        self.transport = transport
        self.broker_id = str(broker_id or "")
        self.security_tier = str(security_tier or "lowpriv")
        self.threat_model = str(threat_model or "T1")
        self.capabilities = dict(capabilities or {})
        self.handler = handler or self._default_handler
        self.journal = journal
        self.lifecycle = lifecycle
        self.grants = grants
        self.consent = consent
        self.device_fingerprint = str(device_fingerprint or "")
        self.require_encrypted = bool(require_encrypted)
        self.hello_nonce_ttl_s = max(1.0, float(hello_nonce_ttl_s))
        self.grant_rate_limit_s = max(0.0, float(grant_rate_limit_s))
        self.max_pending_grants = max(1, int(max_pending_grants))
        self.allowed_ops = frozenset(allowed_ops or PHASE1_REQUEST_OPS)
        self.device_identity = device_identity
        self._running = False
        self._authenticated = False
        self._client_id = ""
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._seen_nonces: dict[str, float] = {}
        self._last_grant_at = 0.0
        self._pending_grants: set[str] = set()

    async def run(self) -> None:
        self._running = True
        try:
            while self._running:
                raw = await self.transport.recv()
                if raw is None:
                    break
                try:
                    frame = decode_frame(raw)
                except ProtocolError:
                    # 畸形帧没有可信 id，直接断开，避免在坏输入上继续。
                    break
                await self._dispatch(frame)
        finally:
            await self._stop_tasks()
            self._running = False
            if self.grants is not None:
                # 断线即撤销：重连后必须重新走本地授权。
                self.grants.clear()

    async def shutdown(self) -> None:
        self._running = False
        await self._stop_tasks()
        if self.grants is not None:
            self.grants.clear()
        try:
            await self.transport.close()
        except Exception:
            pass

    def pause(self) -> None:
        if self.grants is not None:
            self.grants.clear()
        if self.lifecycle is not None:
            self.lifecycle.pause()

    def resume(self) -> None:
        if self.lifecycle is not None:
            self.lifecycle.resume()

    async def _reject_hello(self, frame: Frame, reason: str) -> None:
        self._authenticated = False
        await self._send(make_error(frame, UNAUTHENTICATED, reason))

    def _prune_nonces(self, now: float) -> None:
        for nonce, expires in list(self._seen_nonces.items()):
            if expires <= now:
                self._seen_nonces.pop(nonce, None)

    async def _dispatch(self, frame: Frame) -> None:
        if frame.type == "hello":
            meta = dict(frame.meta or {})
            if int(frame.v or 0) != PROTOCOL_VERSION:
                await self._reject_hello(frame, "协议版本不匹配")
                return
            nonce = str(meta.get("nonce") or "")
            if not nonce or len(nonce) > 128:
                await self._reject_hello(frame, "缺少或非法的 nonce")
                return
            now = time.monotonic()
            self._prune_nonces(now)
            if nonce in self._seen_nonces:
                await self._reject_hello(frame, "hello nonce 重放")
                return
            a2l_id = str(meta.get("a2l_id") or "")
            if a2l_id and a2l_id != self.broker_id:
                await self._reject_hello(frame, "a2l_id 与目标 broker 不匹配")
                return
            encrypted = bool(getattr(self.transport, "is_encrypted", lambda: False)())
            if self.require_encrypted and not encrypted:
                await self._reject_hello(frame, "要求加密通道")
                return
            peer_fp = str(
                getattr(self.transport, "peer_cert_fingerprint", lambda: "")()
            )
            claimed_server_fp = str(meta.get("server_fingerprint") or "")
            if claimed_server_fp and peer_fp and claimed_server_fp != peer_fp:
                await self._reject_hello(frame, "服务端证书指纹不匹配")
                return
            if self.require_encrypted and not peer_fp:
                await self._reject_hello(frame, "缺少对端证书指纹")
                return
            attestation: dict[str, str] = {}
            if self.device_identity is not None:
                try:
                    evidence = self.device_identity.attest(
                        f"{nonce}:{self.broker_id}"
                    )
                    attestation = evidence.to_meta()
                    attestation["attestation_nonce"] = nonce
                except Exception as exc:  # noqa: BLE001 - fail closed
                    await self._reject_hello(
                        frame, f"设备证明失败：{exc}"
                    )
                    return
            self._seen_nonces[nonce] = now + self.hello_nonce_ttl_s
            self._authenticated = True
            self._client_id = str(meta.get("client_id") or "")
            await self._send(
                make_hello_ok(
                    self.broker_id,
                    security_tier=self.security_tier,
                    threat_model=self.threat_model,
                    capabilities=self.capabilities,
                    a2l_id=self.broker_id,
                    device_fingerprint=self.device_fingerprint,
                    nonce=nonce,
                    attestation=attestation,
                )
            )
            return
        if frame.type == "heartbeat":
            await self._send(make_heartbeat())
            return
        if frame.type == "request":
            if not self._authenticated:
                await self._send(make_error(frame, UNAUTHENTICATED, "未握手"))
                return
            task = asyncio.create_task(self._handle_request(frame))
            self._tasks[frame.op_id] = task
            task.add_done_callback(lambda _: self._tasks.pop(frame.op_id, None))
            return
        if frame.type == "cancel":
            task = self._tasks.get(frame.op_id)
            if task is not None:
                task.cancel()
            return
        if frame.type == "grant.request":
            await self._handle_grant_request(frame)
            return
        if frame.type in GRANT_FRAME_TYPES:
            await self._send(
                make_error(
                    frame,
                    GRANT_LOCAL_ONLY,
                    "授权必须由 A2L 本地 UI 完成",
                )
            )
            return
        if frame.type == "event":
            return
        await self._send(make_error(frame, OP_UNKNOWN, f"未知帧：{frame.type}"))

    async def _handle_grant_request(self, frame: Frame) -> None:
        if self.grants is None or self.consent is None:
            await self._send(
                make_error(
                    frame,
                    GRANT_LOCAL_ONLY,
                    "当前 A2L 未配置本地同意 UI",
                )
            )
            return
        now = time.monotonic()
        if (
            len(self._pending_grants) >= self.max_pending_grants
            or (
                self._last_grant_at
                and now - self._last_grant_at < self.grant_rate_limit_s
            )
        ):
            await self._send(
                make_error(
                    frame,
                    GRANT_RATE_LIMITED,
                    "授权请求过于频繁，请稍后再试",
                )
            )
            return
        self._last_grant_at = now
        self._pending_grants.add(frame.id)
        try:
            payload = dict(frame.payload or {})
            try:
                scope = payload.get("scope")
                verbs = payload.get("verbs")
                if not isinstance(scope, list) or not isinstance(verbs, list):
                    raise ValueError("scope/verbs 必须是数组")
                verb_names = {str(item).lower() for item in verbs}
                one_shot = bool(payload.get("one_shot", False)) or bool(
                    verb_names & {"exec", "install"}
                )
                await self._send(make_grant_pending(frame))
                approved = await self.consent.ask(frame)
                if not approved:
                    await self._send(
                        make_grant_denied(frame, reason="本地拒绝")
                    )
                    return
                grant = self.grants.issue(
                    scope=scope,
                    verbs=verbs,
                    ttl_s=int(payload.get("ttl_s", 1800)),
                    reason=str(payload.get("reason") or ""),
                    sensitive=bool(payload.get("sensitive", False)),
                    one_shot=one_shot,
                )
            except ValueError as exc:
                await self._send(make_grant_denied(frame, reason=str(exc)))
                return
            await self._send(
                make_grant_granted(
                    frame,
                    grant_id=grant.grant_id,
                    expires_at=grant.expires_at.isoformat(),
                    one_shot=grant.one_shot,
                )
            )
        finally:
            self._pending_grants.discard(frame.id)

    async def _handle_request(self, frame: Frame) -> None:
        if self.lifecycle is not None and not self.lifecycle.can_serve():
            await self._send(
                make_error(frame, A2L_PAUSED, "A2L 已暂停/离线")
            )
            return
        if frame.kind not in self.allowed_ops:
            await self._send(
                make_error(
                    frame,
                    OP_NOT_ENABLED_IN_PHASE1,
                    f"未开放：{frame.kind}",
                )
            )
            return
        if self.grants is not None and frame.grant_id:
            grant = self.grants.get(frame.grant_id)
            if grant is not None and grant.one_shot:
                if grant.used or not self.grants.claim(frame.grant_id):
                    await self._send(
                        make_error(
                            frame,
                            GRANT_USED,
                            "一次性授权已使用或不可用",
                        )
                    )
                    return
        non_idempotent = frame.kind in NON_IDEMPOTENT_OPS
        started = True
        if self.journal is not None:
            started = self.journal.start(
                frame.op_id,
                frame.kind,
                meta={
                    "workspace": frame.workspace or {},
                    "grant_id": frame.grant_id,
                },
            )
        if not started and non_idempotent:
            # 写/执行/安装不能靠重放确认结果；交给 A3 验证或人工处理。
            existing = (
                self.journal.get(frame.op_id)
                if self.journal is not None
                else None
            )
            await self._send(
                make_error(
                    frame,
                    OP_UNCERTAIN,
                    "该 op_id 已存在，写/执行结果未知，拒绝重放",
                    data={
                        "status": getattr(existing, "status", ""),
                        "kind": getattr(existing, "kind", frame.kind),
                    },
                )
            )
            return
        try:
            result = await self.handler(frame)
            result = with_ids(result, frame)
            await self._send(result)
            if self.journal is not None:
                self.journal.finish(
                    frame.op_id,
                    OP_FINISHED
                    if result.status in {"ok", "manual"}
                    else OP_FAILED,
                    result=result.data,
                )
        except asyncio.CancelledError:
            await self._send(
                make_response(
                    frame,
                    status="cancelled",
                    error_code=CANCELLED,
                    error="操作已取消",
                )
            )
            if self.journal is not None:
                self.journal.finish(
                    frame.op_id,
                    OP_UNKNOWN if non_idempotent else OP_CANCELLED,
                )
            return
        except HandlerError as exc:
            await self._send(
                make_error(
                    frame,
                    exc.code,
                    exc.message,
                    data=getattr(exc, "data", None),
                )
            )
            if self.journal is not None:
                if exc.code in PRE_EXECUTION_DENIALS:
                    # 这类错误在写入/执行前返回，允许换 grant 后用新 op_id 重试。
                    self.journal.discard(frame.op_id)
                else:
                    self.journal.finish(
                        frame.op_id,
                        OP_UNKNOWN if non_idempotent else OP_FAILED,
                        result={"error": exc.message},
                    )
        except Exception as exc:  # noqa: BLE001 - 边界处转成协议错误
            await self._send(make_error(frame, INTERNAL, str(exc)))
            if self.journal is not None:
                self.journal.finish(
                    frame.op_id,
                    OP_UNKNOWN if non_idempotent else OP_FAILED,
                    result={"error": str(exc)},
                )

    async def _default_handler(self, frame: Frame) -> Frame:
        if frame.kind == "host.health":
            return make_response(
                frame,
                data={
                    "broker_id": self.broker_id,
                    "security_tier": self.security_tier,
                    "threat_model": self.threat_model,
                    "capabilities": self.capabilities,
                },
            )
        raise HandlerError(
            OP_NOT_ENABLED_IN_PHASE1, f"Phase 1 未开放：{frame.kind}"
        )

    async def _send(self, frame: Frame) -> None:
        try:
            await self.transport.send(encode_frame(frame))
        except Exception:
            self._running = False

    async def _stop_tasks(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
