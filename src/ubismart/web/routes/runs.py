"""运行生命周期路由：run / steer / resume / status / stream / history。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ubismart.agent_protocol import AgentRunRequest
from ubismart.config import Settings
from ubismart.control.idempotency import IdempotencyInFlight
from ubismart.control.leases import LeaseHeartbeat
from ubismart.control.webhooks import schedule_webhooks
from ubismart.identity.tenancy import WebTenant
from ubismart.platform.observability import inc_gauge, note_silent_failure
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import UserDep
from ubismart.web.models import ResumeRequest, RunRequest, SteerRequest
from ubismart.web.push import schedule_push
from ubismart.web.routes.notifications import notification_payload
from ubismart.web.transcripts import (
    clean_transcript,
    load_transcript,
    save_transcript,
)
from ubismart.web.validation import check_thread_id

_LOG = logging.getLogger(__name__)


def _swallow(site: str, exc: BaseException) -> None:
    """best-effort 分支的统一出口：不再静默。

    以前是 `except Exception: pass`：计费泄漏、审计漏写、租约残留都看不见。
    现在每吞一次都计数（`agent_silent_failures_total{site}`）并记 warning——
    吞掉异常本身可以是设计选择（不该让主流程失败），**不留下痕迹不行**。
    """
    note_silent_failure(site)
    _LOG.warning("%s 的 best-effort 步骤失败，已吞掉：%s", site, exc)


@dataclass(frozen=True)
class RunDeps:
    settings: Settings
    client_source: str
    stream_buffer: bool
    transport: Any
    streams: dict[tuple[str, str], Any]
    registry_lock: Any
    runtimes: Any
    rate_limit: Callable[[AuthUser], None]
    sweep_expired: Callable[[], Awaitable[None]]
    tenant: Callable[[AuthUser], WebTenant]
    config_for: Callable[..., Any]
    set_config: Callable[[AuthUser, WebTenant, Any], None]
    reject_deleted: Callable[[AuthUser, str], None]
    agent_client: Any
    agent_context: Callable[..., Any]
    agent_failure: Callable[[Exception], HTTPException]
    proxy_agent: Callable[..., Awaitable[StreamingResponse]]
    stream_guard: Callable[..., Any]
    start_stream: Callable[..., Any]
    buffered_sse: Callable[..., Any]
    sse_response: Callable[..., Any]
    get_entry: Callable[[str, str], Any]
    runtime_for_resume: Callable[..., Awaitable[tuple[Any, Any]]]
    new_runtime: Callable[..., Any]
    event_key: Callable[[str, str], str]
    transport_sse: Callable[..., Any]
    ops_runtime: Callable[[WebTenant, Any], Any]
    # M4：跨 A2/A3 重启的运行租约；None = 未配置控制面数据库。
    run_leases: Any | None = None
    lease_owner: str = ""
    lease_ttl_s: int = 900
    audit_flush: Callable[..., Awaitable[None]] | None = None
    idempotency: Any | None = None
    # 企业每日额度；None = 未启用控制面。
    control_store: Any | None = None
    quota_reserve_tokens: int = 0
    usd_to_cny: float = 7.2
    quota_owner: str = ""
    quota_ttl_s: int = 3600
    # RFC-0004：四眼审批；None / 0 = 关闭。
    approvals: Any | None = None
    four_eyes_min_level: int = 0
    record_approval: Callable[..., None] | None = None
    # 浏览器 Web Push；None = 未启用。
    push: Any | None = None
    # 出站 Webhook；None = 未启用。
    webhooks: Any | None = None


def _lease_key(user: AuthUser, public_thread: str) -> str:
    return f"{user.name}:{public_thread}"


def lease_conflict_detail(
    public_thread: str,
    idempotency_key: str = "",
    active: dict | None = None,
) -> dict:
    detail = {
        "code": "run_in_progress",
        "thread_id": public_thread,
        "stream_url": f"/api/stream/{public_thread}",
    }
    if (
        idempotency_key
        and active
        and str(active.get("lease_id") or "") == idempotency_key
    ):
        detail["idempotent"] = True
    return detail


def _is_terminal_chunk(chunk: bytes) -> bool:
    text = chunk.decode("utf-8", errors="replace")
    return (
        '"type": "run_finished"' in text
        or '"type": "run_failed"' in text
        or '"type": "approval_requested"' in text
        or '"type":"run_finished"' in text
        or '"type":"run_failed"' in text
        or '"type":"approval_requested"' in text
        or ('"type": "steering_applied"' in text and '"pause"' in text)
        or ('"type":"steering_applied"' in text and '"pause"' in text)
    )


def register_run_routes(app: FastAPI, deps: RunDeps) -> None:
    def _idem_begin(key: str, scope: str) -> dict | None:
        if deps.idempotency is None or not str(key or "").strip():
            return None
        try:
            return deps.idempotency.begin(
                key,
                scope,
                ttl_s=int(deps.settings.idempotency_ttl_s or 600),
            )
        except IdempotencyInFlight as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _idem_finish(key: str, result: dict) -> None:
        if deps.idempotency is not None and str(key or "").strip():
            deps.idempotency.finish(
                key,
                result,
                ttl_s=int(deps.settings.idempotency_ttl_s or 600),
            )

    def _idem_abort(key: str) -> None:
        if deps.idempotency is not None and str(key or "").strip():
            deps.idempotency.abort(key)

    async def _reserve_quota(user: AuthUser) -> dict[str, Any]:
        if deps.control_store is None:
            return {}
        day = datetime.now(UTC).date().isoformat()
        try:
            reserved = await asyncio.to_thread(
                deps.control_store.reserve_quota,
                user.tenant,
                user.name,
                day,
                tokens=max(0, int(deps.quota_reserve_tokens or 0)),
                cost_usd=max(0.0, float(deps.settings.run_cost_budget_usd or 0)),
                usd_to_cny=float(deps.usd_to_cny or 1),
                owner=deps.quota_owner,
                ttl_s=int(deps.quota_ttl_s or 3600),
            )
        except Exception as exc:  # noqa: BLE001
            from ubismart.control.store import QuotaExceeded

            if isinstance(exc, QuotaExceeded):
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "quota.exceeded",
                        "message": "每日额度已用尽",
                        "details": {
                            **exc.details,
                            "reset_at": "次日 00:00 UTC",
                        },
                    },
                ) from exc
            raise
        if not reserved:
            return {}
        return {**reserved, "day": day}

    async def _release_quota(
        user: AuthUser, reservation: dict[str, Any]
    ) -> None:
        if not reservation or deps.control_store is None:
            return
        try:
            await asyncio.to_thread(
                deps.control_store.settle_quota,
                str(reservation.get("id") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 释放失败不应影响响应
            # 结算不回去 = 用户的额度被永久占用，这是账单级问题，不能静默。
            _swallow("quota_settle", exc)

    def _finalize_stream(
        response: StreamingResponse,
        user: AuthUser,
        tenant: WebTenant | None,
        config: Any,
        reservation: dict[str, Any],
    ) -> StreamingResponse:
        """流结束后释放配额并拉取审计，保证本地运行也能计量。"""
        original = response.body_iterator
        inc_gauge("agent_active_runs", 1)

        async def body() -> Any:
            try:
                async for chunk in original:
                    yield chunk
            finally:
                inc_gauge("agent_active_runs", -1)
                if deps.audit_flush is not None and tenant is not None:
                    try:
                        await deps.audit_flush(user, tenant, config)
                    except Exception as exc:  # noqa: BLE001 - 计量失败不阻断响应
                        # 漏记用量 = 少计费；留痕才可能对账补记。
                        _swallow("usage_flush", exc)
                await _release_quota(user, reservation)

        response.body_iterator = body()
        return response

    def _acquire_lease(
        user: AuthUser, public_thread: str, idempotency_key: str
    ) -> str:
        if deps.run_leases is None:
            return ""
        from ubismart.control.leases import LeaseHeld

        try:
            return deps.run_leases.acquire(
                _lease_key(user, public_thread),
                deps.lease_owner,
                ttl_s=deps.lease_ttl_s,
                lease_id=idempotency_key,
            )
        except LeaseHeld as exc:
            key = _lease_key(user, public_thread)
            active = None
            try:
                active = deps.run_leases.active(key)
            except Exception as exc:  # noqa: BLE001 - 冲突详情失败不影响拒绝
                _swallow("lease_conflict_detail", exc)
            raise HTTPException(
                status_code=409,
                detail=lease_conflict_detail(
                    public_thread, idempotency_key, active
                ),
            ) from exc

    def _release_lease(
        user: AuthUser, public_thread: str, lease_id: str
    ) -> None:
        if deps.run_leases is None or not lease_id:
            return
        try:
            deps.run_leases.release(
                _lease_key(user, public_thread), lease_id
            )
        except Exception as exc:  # noqa: BLE001 - 释放失败交给 TTL
            # 租约残留会让会话在 TTL 内无法重跑，必须留痕。
            _swallow("run_lease_release", exc)

    def _lease_callbacks(
        user: AuthUser,
        public_thread: str,
        lease_id: str,
        tenant: WebTenant | None = None,
        config: Any | None = None,
    ) -> tuple[Callable[[], None], Callable[[], None]]:
        last = {"at": 0.0}

        def progress() -> None:
            if deps.run_leases is None or not lease_id:
                return
            now = time.monotonic()
            if now - last["at"] < 30:
                return
            last["at"] = now
            try:
                deps.run_leases.renew(
                    _lease_key(user, public_thread),
                    lease_id,
                    ttl_s=deps.lease_ttl_s,
                )
            except Exception as exc:  # noqa: BLE001 - 续租失败交给 TTL
                _swallow("run_lease_renew", exc)

        def terminal() -> None:
            _release_lease(user, public_thread, lease_id)
            if (
                deps.audit_flush is not None
                and tenant is not None
            ):
                try:
                    asyncio.create_task(
                        deps.audit_flush(user, tenant, config)
                    )
                except RuntimeError:
                    pass

        return progress, terminal

    async def _start_heartbeat(
        user: AuthUser, public_thread: str, lease_id: str
    ) -> LeaseHeartbeat | None:
        if not lease_id or deps.run_leases is None:
            return None
        heartbeat = LeaseHeartbeat(
            deps.run_leases,
            _lease_key(user, public_thread),
            lease_id,
            ttl_s=deps.lease_ttl_s,
        )
        await heartbeat.start()
        return heartbeat

    def _with_heartbeat(
        heartbeat: LeaseHeartbeat | None,
        terminal: Callable[[], None],
    ) -> Callable[[], None]:
        if heartbeat is None:
            return terminal

        def wrapped() -> None:
            heartbeat.stop_now()
            terminal()

        return wrapped

    async def _merge_approval_decisions(
        user: AuthUser,
        public_thread: str,
        decisions: dict[str, bool],
    ) -> dict[str, bool]:
        if deps.approvals is None or deps.four_eyes_min_level <= 0:
            return dict(decisions)
        return await asyncio.to_thread(
            deps.approvals.merge_decisions,
            tenant=user.tenant,
            thread_id=public_thread,
            owner_decisions=dict(decisions),
            min_level=int(deps.four_eyes_min_level),
        )

    async def _write_notification(
        user: AuthUser, public_thread: str, payload: dict[str, Any]
    ) -> None:
        store = deps.control_store
        if store is None:
            return
        notice = notification_payload(payload)
        if notice is None:
            return
        kind, title, body, dedupe = notice
        try:
            row = await asyncio.to_thread(
                store.add_notification,
                user.name,
                kind=kind,
                title=title,
                body=body,
                thread_id=public_thread,
                dedupe_key=dedupe,
            )
            if row:
                schedule_push(deps.push, user.name, row)
                schedule_webhooks(
                    deps.webhooks,
                    user.name,
                    str(payload.get("type") or ""),
                    {
                        "title": title,
                        "body": body,
                        "kind": kind,
                        "thread_id": public_thread,
                        "created_at": row.get("created_at", ""),
                    },
                )
        except Exception as exc:  # noqa: BLE001 - 通知失败不打断运行
            _swallow("notification", exc)

    async def _proxy_agent_stream(
        user: AuthUser,
        public_thread: str,
        tenant: WebTenant,
        config: Any,
        reservation: dict[str, Any],
        *,
        acquire: Callable[[], str],
        open_stream: Callable[[], Awaitable[Any]],
    ) -> StreamingResponse:
        """run/resume/resume_steering 共用：租约 + 心跳 + 代理 + 释放。"""
        lease_id = ""
        heartbeat: LeaseHeartbeat | None = None

        async def _on_event(payload: dict[str, Any]) -> None:
            await _write_notification(user, public_thread, payload)
            if payload.get("type") != "approval_requested":
                return
            recorder = deps.record_approval
            if recorder is None:
                return
            await asyncio.to_thread(
                recorder, user, tenant, public_thread, payload
            )

        event_hook = (
            _on_event
            if deps.control_store is not None
            or (
                deps.record_approval is not None
                and deps.four_eyes_min_level > 0
            )
            else None
        )
        try:
            lease_id = acquire()
            heartbeat = await _start_heartbeat(
                user, public_thread, lease_id
            )
            stream = await open_stream()
        except Exception as exc:  # noqa: BLE001
            if heartbeat is not None:
                await heartbeat.stop()
            if lease_id:
                _release_lease(user, public_thread, lease_id)
            await _release_quota(user, reservation)
            if isinstance(exc, HTTPException):
                raise
            raise deps.agent_failure(exc) from exc
        progress, terminal = _lease_callbacks(
            user, public_thread, lease_id, tenant, config
        )
        terminal = _with_heartbeat(heartbeat, terminal)
        try:
            response = await deps.proxy_agent(
                stream,
                public_thread=public_thread,
                on_progress=progress,
                on_terminal=terminal,
                on_event=event_hook,
            )
        except Exception:
            if heartbeat is not None:
                await heartbeat.stop()
            await _release_quota(user, reservation)
            raise
        return _finalize_stream(
            response, user, tenant, config, reservation
        )

    @app.get("/api/approvals/{thread_id}")
    async def pending_approvals(
        thread_id: str,
        user: UserDep,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        await asyncio.to_thread(deps.reject_deleted, user, public_thread)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            config = await asyncio.to_thread(
                deps.config_for, user, tenant, public_thread, create=False
            )
            try:
                return await deps.agent_client.approvals(
                    deps.agent_context(
                        user, tenant, config, include_keys=True
                    ),
                    public_thread,
                )
            except Exception as exc:  # noqa: BLE001
                raise deps.agent_failure(exc) from exc
        entry = deps.get_entry(user.name, public_thread)
        temporary = None
        runtime = entry.runtime if entry is not None else None
        if runtime is None:
            config = await asyncio.to_thread(
                deps.config_for, user, tenant, public_thread
            )
            temporary = deps.new_runtime(tenant, config)
            runtime = temporary
        try:
            method = getattr(runtime, "pending_approvals", None)
            if not callable(method):
                return {"approvals": []}
            found = await method(tenant.thread_id(public_thread))
            return {
                "approvals": [
                    item.model_dump() if hasattr(item, "model_dump") else item
                    for item in found
                ]
            }
        except Exception as exc:  # noqa: BLE001 - 跨进程恢复失败要可读报出
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            if temporary is not None:
                await temporary.aclose()

    @app.post("/api/run")
    async def run(
        request: RunRequest,
        user: UserDep,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key")
        ] = "",
    ) -> StreamingResponse:
        deps.rate_limit(user)
        await deps.sweep_expired()
        if len(request.prompt) > deps.settings.web_max_prompt_chars:
            raise HTTPException(
                status_code=413,
                detail=(
                    "prompt 超过上限 "
                    f"{deps.settings.web_max_prompt_chars} 字符"
                ),
            )
        public_thread = check_thread_id(request.thread_id)
        await asyncio.to_thread(deps.reject_deleted, user, public_thread)
        if deps.agent_client is not None:
            tenant = deps.tenant(user)
            config = await asyncio.to_thread(
                deps.config_for, user, tenant, public_thread
            )
            if not config.title:
                config.title = " ".join(request.prompt.split())[:120]
            config.last_message = " ".join(request.prompt.split())[:160]
            config.unread = False
            await asyncio.to_thread(deps.set_config, user, tenant, config)
            # 配额必须在远端启动运行之前扣：失败时不能留下已启动的孤儿 run。
            reservation = await _reserve_quota(user)
            return await _proxy_agent_stream(
                user,
                public_thread,
                tenant,
                config,
                reservation,
                acquire=lambda: _acquire_lease(
                    user, public_thread, idempotency_key
                ),
                open_stream=lambda: deps.agent_client.open_run(
                    AgentRunRequest(
                        context=deps.agent_context(
                            user, tenant, config, include_keys=True
                        ),
                        thread_id=public_thread,
                        prompt=request.prompt,
                    )
                ),
            )
        key = (user.name, public_thread)
        with deps.registry_lock:
            if deps.runtimes.contains(key):
                raise HTTPException(
                    status_code=409,
                    detail="该会话已挂起或正在运行；请先 resume",
                )
        deps.stream_guard(key)
        tenant = deps.tenant(user)
        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread
        )
        config.last_message = " ".join(request.prompt.split())[:160]
        config.unread = False
        await asyncio.to_thread(deps.set_config, user, tenant, config)
        runtime = deps.new_runtime(tenant, config)
        with deps.registry_lock:
            deps.runtimes.put(key, runtime)
        events = runtime.run(
            request.prompt,
            thread_id=tenant.thread_id(public_thread),
        )
        reservation = await _reserve_quota(user)
        try:
            if deps.stream_buffer:
                response = deps.buffered_sse(
                    deps.start_stream(key, public_thread, runtime, events)
                )
            else:
                response = deps.sse_response(
                    key,
                    public_thread,
                    runtime,
                    events,
                )
        except Exception:
            await _release_quota(user, reservation)
            raise
        return _finalize_stream(
                response, user, tenant, config, reservation
            )

    @app.post("/api/steer/{thread_id}")
    async def steer(
        thread_id: str,
        request: SteerRequest,
        user: UserDep,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key")
        ] = "",
    ) -> dict:
        deps.rate_limit(user)
        public_thread = check_thread_id(thread_id)
        cached = _idem_begin(idempotency_key, "run.steer")
        if cached is not None:
            return cached
        try:
            if deps.agent_client is not None:
                tenant = deps.tenant(user)
                config = await asyncio.to_thread(
                    deps.config_for, user, tenant, public_thread, create=False
                )
                result = await deps.agent_client.steer(
                    deps.agent_context(user, tenant, config),
                    public_thread,
                    action=request.action,
                    text=request.text,
                    role=request.role,
                )
            else:
                entry = deps.get_entry(user.name, public_thread)
                if entry is None:
                    raise HTTPException(
                        status_code=404,
                        detail="该会话没有运行中的 agent",
                    )
                tenant = deps.tenant(user)
                try:
                    entry.runtime.steer(
                        tenant.thread_id(public_thread),
                        request.action,
                        text=request.text,
                        role=request.role,
                        source=deps.client_source,
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=str(exc)
                    ) from exc
                result = {"ok": True, "action": request.action}
        except HTTPException:
            _idem_abort(idempotency_key)
            raise
        except Exception as exc:  # noqa: BLE001
            _idem_abort(idempotency_key)
            raise deps.agent_failure(exc) from exc
        _idem_finish(idempotency_key, result)
        return result

    @app.post("/api/resume/{thread_id}")
    async def resume(
        thread_id: str,
        request: ResumeRequest,
        user: UserDep,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key")
        ] = "",
    ) -> StreamingResponse:
        deps.rate_limit(user)
        await deps.sweep_expired()
        public_thread = check_thread_id(thread_id)
        await asyncio.to_thread(deps.reject_deleted, user, public_thread)
        if deps.agent_client is not None:
            tenant = deps.tenant(user)
            config = await asyncio.to_thread(
                deps.config_for, user, tenant, public_thread, create=False
            )
            reservation = await _reserve_quota(user)
            decisions = await _merge_approval_decisions(
                user, public_thread, request.decisions
            )
            return await _proxy_agent_stream(
                user,
                public_thread,
                tenant,
                config,
                reservation,
                acquire=lambda: _acquire_lease(
                    user, public_thread, idempotency_key
                ),
                open_stream=lambda: deps.agent_client.open_resume(
                    deps.agent_context(
                        user, tenant, config, include_keys=True
                    ),
                    public_thread,
                    decisions,
                ),
            )
        key = (user.name, public_thread)
        existing = deps.get_entry(user.name, public_thread)
        if existing is not None and getattr(
            existing.runtime, "_stream_active", False
        ):
            raise HTTPException(
                status_code=409,
                detail="该会话正在运行；不能同时 resume",
            )
        deps.stream_guard(key, require_suspended=True)
        key, runtime = await deps.runtime_for_resume(user, public_thread)
        deps.stream_guard(key, require_suspended=True)
        tenant = deps.tenant(user)
        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread, create=False
        )
        internal = tenant.thread_id(public_thread)
        if runtime.is_paused(internal):
            raise HTTPException(
                status_code=409,
                detail="该会话是 steering 暂停；请用 /api/resume_steering",
            )
        events = runtime.resume(internal, request.decisions)
        reservation = await _reserve_quota(user)
        try:
            if deps.stream_buffer:
                response = deps.buffered_sse(
                    deps.start_stream(key, public_thread, runtime, events)
                )
            else:
                response = deps.sse_response(key, public_thread, runtime, events)
        except Exception:
            await _release_quota(user, reservation)
            raise
        return _finalize_stream(
                response, user, tenant, config, reservation
            )

    @app.post("/api/resume_steering/{thread_id}")
    async def resume_steering(
        thread_id: str,
        user: UserDep,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key")
        ] = "",
    ) -> StreamingResponse:
        deps.rate_limit(user)
        await deps.sweep_expired()
        public_thread = check_thread_id(thread_id)
        await asyncio.to_thread(deps.reject_deleted, user, public_thread)
        if deps.agent_client is not None:
            tenant = deps.tenant(user)
            config = await asyncio.to_thread(
                deps.config_for, user, tenant, public_thread, create=False
            )
            reservation = await _reserve_quota(user)
            return await _proxy_agent_stream(
                user,
                public_thread,
                tenant,
                config,
                reservation,
                acquire=lambda: _acquire_lease(
                    user, public_thread, idempotency_key
                ),
                open_stream=lambda: deps.agent_client.open_resume_steering(
                    deps.agent_context(
                        user, tenant, config, include_keys=True
                    ),
                    public_thread,
                ),
            )
        key = (user.name, public_thread)
        deps.stream_guard(key)
        key, runtime = await deps.runtime_for_resume(user, public_thread)
        deps.stream_guard(key)
        tenant = deps.tenant(user)
        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread, create=False
        )
        internal = tenant.thread_id(public_thread)
        if not runtime.is_paused(internal):
            raise HTTPException(
                status_code=409,
                detail=(
                    "该会话未处于 steering 暂停状态；"
                    "审批恢复请用 /api/resume"
                ),
            )
        events = runtime.resume_steering(internal)
        reservation = await _reserve_quota(user)
        try:
            if deps.stream_buffer:
                response = deps.buffered_sse(
                    deps.start_stream(key, public_thread, runtime, events)
                )
            else:
                response = deps.sse_response(key, public_thread, runtime, events)
        except Exception:
            await _release_quota(user, reservation)
            raise
        return _finalize_stream(
                response, user, tenant, config, reservation
            )

    @app.get("/api/status/{thread_id}")
    async def status(
        thread_id: str,
        user: UserDep,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        if deps.agent_client is not None:
            tenant = deps.tenant(user)
            config = await asyncio.to_thread(
                deps.config_for, user, tenant, public_thread, create=False
            )
            try:
                return await deps.agent_client.status(
                    deps.agent_context(user, tenant, config), public_thread
                )
            except Exception as exc:  # noqa: BLE001
                raise deps.agent_failure(exc) from exc
        entry = deps.get_entry(user.name, public_thread)
        stream_entry = deps.streams.get((user.name, public_thread))
        extra = (
            {
                "streaming": bool(stream_entry and not stream_entry.done),
                "last_seq": stream_entry.seq if stream_entry else 0,
            }
            if stream_entry is not None
            else {}
        )
        if entry is None:
            return {
                "exists": False,
                "thread_id": public_thread,
                "paused": False,
                **extra,
            }
        tenant = deps.tenant(user)
        return {
            "exists": True,
            "thread_id": public_thread,
            "paused": bool(
                entry.runtime.is_paused(tenant.thread_id(public_thread))
            ),
            **extra,
        }

    @app.get("/api/stream/{thread_id}")
    async def stream_events(
        thread_id: str,
        request: Request,
        user: UserDep,
        after: int = 0,
    ) -> Response:
        """重连到事件流；`Last-Event-ID` 或 `after` 指定起点。"""
        if deps.agent_client is None and (
            not deps.stream_buffer or deps.transport is None
        ):
            raise HTTPException(status_code=404, detail="事件重放未启用")
        public_thread = check_thread_id(thread_id)
        header = request.headers.get("last-event-id", "")
        try:
            header_seq = int(header)
        except (TypeError, ValueError):
            header_seq = 0
        start = max(0, int(after), header_seq)
        if deps.agent_client is not None:
            tenant = deps.tenant(user)
            config = await asyncio.to_thread(
                deps.config_for, user, tenant, public_thread, create=False
            )
            try:
                stream = await deps.agent_client.open_stream(
                    deps.agent_context(user, tenant, config),
                    public_thread,
                    after=start,
                )
            except Exception as exc:  # noqa: BLE001
                raise deps.agent_failure(exc) from exc
            if stream.status_code == 204:
                await stream.aclose()
                return Response(status_code=204)
            return await deps.proxy_agent(
                stream, public_thread=public_thread
            )
        event_key = deps.event_key(user.name, public_thread)
        entry = deps.streams.get((user.name, public_thread))
        if entry is not None:
            try:
                latest = deps.transport.latest_seq(event_key)
            except Exception:  # noqa: BLE001 - 传输不可用时退回本地缓冲
                latest = entry.seq
            if latest > entry.seq:
                return deps.transport_sse(
                    event_key, public_thread, after=start
                )
            return deps.buffered_sse(entry, after=start)
        try:
            latest = deps.transport.latest_seq(event_key)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503, detail="事件传输不可用"
            ) from exc
        if latest <= start:
            return Response(status_code=204)
        return deps.transport_sse(event_key, public_thread, after=start)

    @app.get("/api/thread/{thread_id}")
    async def thread_history(
        thread_id: str,
        user: UserDep,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        tenant = deps.tenant(user)
        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread, create=False
        )
        transcript, transcript_has_more = load_transcript(
            tenant,
            public_thread,
            limit=limit,
            offset=offset,
        )
        if deps.agent_client is not None:
            try:
                data = await deps.agent_client.history(
                    deps.agent_context(user, tenant, config),
                    public_thread,
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:  # noqa: BLE001
                raise deps.agent_failure(exc) from exc
            if isinstance(data, dict):
                data["transcript"] = transcript
                data["transcript_has_more"] = transcript_has_more
            return data
        runtime = deps.ops_runtime(tenant, config)
        try:
            history = await runtime.history(
                tenant.thread_id(public_thread),
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001 - 拿不到历史不该让页面报错
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {
            "messages": [
                {"role": message.role, "text": message.text}
                for message in history
            ],
            "transcript": transcript,
            "transcript_has_more": transcript_has_more,
        }

    @app.post("/api/thread/{thread_id}/transcript")
    async def save_thread_transcript(
        thread_id: str,
        request: Request,
        user: UserDep,
    ) -> dict:
        """保存桌面 UI 的完整对话轨迹（含工具卡片与过程中间态）。"""
        deps.rate_limit(user)
        public_thread = check_thread_id(thread_id)
        await asyncio.to_thread(deps.reject_deleted, user, public_thread)
        tenant = deps.tenant(user)
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="请求体不是 JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是对象")
        messages = clean_transcript(payload.get("messages"))
        try:
            save_transcript(tenant, public_thread, messages)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"保存对话轨迹失败：{exc}"
            ) from exc
        preview = ""
        for item in reversed(messages):
            if item.get("kind") == "tool":
                continue
            text = " ".join(str(item.get("text") or "").split())
            if text:
                preview = text[:160]
                break
        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread, create=False
        )
        if config is not None:
            if preview:
                config.last_message = preview
            if isinstance(payload.get("unread"), bool):
                config.unread = bool(payload.get("unread"))
            await asyncio.to_thread(deps.set_config, user, tenant, config)
        store = deps.control_store
        if store is not None:
            body = "\n".join(
                " ".join(str(item.get("text") or "").split())
                for item in messages
                if item.get("kind") != "tool"
                and str(item.get("text") or "").strip()
            )[:20000]
            try:
                await asyncio.to_thread(
                    store.index_session_text,
                    user.name,
                    public_thread,
                    title=(config.title if config else ""),
                    body=body,
                )
            except Exception as exc:  # noqa: BLE001 - 搜索索引不阻断保存
                _swallow("search_index", exc)
        return {"ok": True, "count": len(messages)}
