"""A2L 服务端骨架：握手、请求分发、取消、暂停与审计。

传输与权限判定由外部注入；本模块只负责协议状态机，不直接读写宿主文件。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from coding_agent.host_broker.consent import ConsentProvider
from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.op_journal import (
    OP_CANCELLED,
    OP_FAILED,
    OP_FINISHED,
    OpJournal,
)
from coding_agent.host_broker.policy import GrantStore
from coding_agent.host_broker.protocol import (
    A2L_PAUSED,
    CANCELLED,
    GRANT_LOCAL_ONLY,
    INTERNAL,
    OP_NOT_ENABLED_IN_PHASE1,
    OP_UNKNOWN,
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
from coding_agent.host_broker.transport import Transport

PHASE1_REQUEST_OPS = frozenset(
    {
        "host.health",
        "host.workspace.register",
        "host.list_dir",
        "host.find_files",
        "host.stat",
        "host.read_text",
        "host.toolchains",
        "host.env.inspect",
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
        self._running = False
        self._authenticated = False
        self._client_id = ""
        self._tasks: dict[str, asyncio.Task[None]] = {}

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

    async def shutdown(self) -> None:
        self._running = False
        await self._stop_tasks()
        try:
            await self.transport.close()
        except Exception:
            pass

    def pause(self) -> None:
        if self.lifecycle is not None:
            self.lifecycle.pause()

    def resume(self) -> None:
        if self.lifecycle is not None:
            self.lifecycle.resume()

    async def _dispatch(self, frame: Frame) -> None:
        if frame.type == "hello":
            self._authenticated = True
            self._client_id = str(frame.meta.get("client_id") or "")
            await self._send(
                make_hello_ok(
                    self.broker_id,
                    security_tier=self.security_tier,
                    threat_model=self.threat_model,
                    capabilities=self.capabilities,
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
        payload = dict(frame.payload or {})
        try:
            scope = payload.get("scope")
            verbs = payload.get("verbs")
            if not isinstance(scope, list) or not isinstance(verbs, list):
                raise ValueError("scope/verbs 必须是数组")
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
            )
        except ValueError as exc:
            await self._send(make_grant_denied(frame, reason=str(exc)))
            return
        await self._send(
            make_grant_granted(
                frame,
                grant_id=grant.grant_id,
                expires_at=grant.expires_at.isoformat(),
            )
        )

    async def _handle_request(self, frame: Frame) -> None:
        if self.lifecycle is not None and not self.lifecycle.can_serve():
            await self._send(
                make_error(frame, A2L_PAUSED, "A2L 已暂停/离线")
            )
            return
        if frame.kind not in PHASE1_REQUEST_OPS:
            await self._send(
                make_error(
                    frame,
                    OP_NOT_ENABLED_IN_PHASE1,
                    f"Phase 1 未开放：{frame.kind}",
                )
            )
            return
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
        if not started:
            # 只读 op 可以安全重试；写/执行阶段必须改为 unknown 语义。
            pass
        try:
            result = await self.handler(frame)
            result = with_ids(result, frame)
            await self._send(result)
            if self.journal is not None:
                self.journal.finish(
                    frame.op_id,
                    OP_FINISHED if result.status == "ok" else OP_FAILED,
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
                self.journal.finish(frame.op_id, OP_CANCELLED)
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
                self.journal.finish(
                    frame.op_id, OP_FAILED, result={"error": exc.message}
                )
        except Exception as exc:  # noqa: BLE001 - 边界处转成协议错误
            await self._send(make_error(frame, INTERNAL, str(exc)))
            if self.journal is not None:
                self.journal.finish(
                    frame.op_id, OP_FAILED, result={"error": str(exc)}
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
