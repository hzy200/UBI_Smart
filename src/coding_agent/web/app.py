"""Web 验证界面。

面向公网/多用户的约束：
- 所有 `/api/*`（除公开 `/healthz`）都要求 Bearer Token 或 loopback 匿名；
- runtime 按 `(user, thread)` 隔离，不能跨用户读取/恢复；
- workspace / audit / checkpoint / steering / memory 均按租户隔离；
- 单进程运行：WSL/LLM/worker 配额由进程级 governor 控制；
- 安全头、请求体上限、按用户限流在中间件/依赖层完成。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from coding_agent.access import ACCESS_READ_ONLY, normalize_access_mode
from coding_agent.agent_protocol import AgentContext
from coding_agent.concurrency import ConcurrencyGovernor
from coding_agent.config import Settings, apply_security_profile, get_settings
from coding_agent.control.agent_client import AgentClientError
from coding_agent.events import (
    ApprovalRequested,
    Event,
    RunFailed,
    SteeringApplied,
)
from coding_agent.memory.sessions import SessionIndex
from coding_agent.provider_profiles import (
    ProviderProfileStore,
    merge_provider_items,
)
from coding_agent.secrets_store import SecretStore
from coding_agent.tenancy import TenantError, TenantManager, WebTenant
from coding_agent.web.auth import AuthConfig, AuthUser
from coding_agent.web.local_runtime import LocalRuntimeRegistry, RuntimeEntry
from coding_agent.web.middleware import (
    BodySizeLimitMiddleware,
    FixedWindowRateLimiter,
    SecurityHeadersMiddleware,
)
from coding_agent.web.models import (
    SessionConfig,
)
from coding_agent.web.routes.compact import CompactDeps, register_compact_routes
from coding_agent.web.routes.desktop import (
    DesktopDeps,
    register_desktop_routes,
)
from coding_agent.web.routes.ops import OpsDeps, register_ops_routes
from coding_agent.web.routes.providers import ProviderDeps, register_provider_routes
from coding_agent.web.routes.runs import RunDeps, register_run_routes
from coding_agent.web.routes.sessions import (
    SessionDeps,
    register_session_routes,
)
from coding_agent.web.routes.snapshots import (
    SnapshotDeps,
    register_snapshot_routes,
)
from coding_agent.web.routes.system import SystemDeps, register_system_routes
from coding_agent.web.routes.uploads import (
    UploadDeps,
    register_upload_routes,
)
from coding_agent.web.routes.workspace import (
    WorkspaceDeps,
    register_workspace_routes,
)

STATIC_DIR = Path(__file__).parent / "static"
_LOG = logging.getLogger(__name__)

WEB_APPROVAL_MODE = "ask"
# 每个会话的重放缓冲上限；超出后丢最旧事件（序号仍连续）。
_STREAM_BUFFER_MAX_CHARS = 8 * 1024 * 1024
# A2 周期探测 A3；A3 重启后状态自动从 unavailable 回到 ok。
AGENT_PROBE_INTERVAL_S = 5.0


@dataclass
class StreamEntry:
    """一次运行的事件缓冲；HTTP 连接只是订阅者，不再拥有运行本身。"""

    key: tuple[str, str]
    public_thread: str
    runtime: Any
    events: list[tuple[int, Event]] = field(default_factory=list)
    chars: int = 0
    seq: int = 0
    done: bool = False
    suspended: bool = False
    updated: float = field(default_factory=time.monotonic)
    task: Any = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)


def _sse(event: Event) -> str:
    """一条领域事件 → 一个 SSE 数据帧。事件本身就是 pydantic 模型，直接序列化。"""
    return f"data: {event.model_dump_json()}\n\n"


def _sse_with_id(event: Event, seq: int) -> str:
    """缓冲模式帧：带事件序号，便于 `Last-Event-ID` 断线重放。"""
    return f"id: {seq}\ndata: {event.model_dump_json()}\n\n"


def _is_suspended(event: Event) -> bool:
    return isinstance(event, ApprovalRequested) or (
        isinstance(event, SteeringApplied) and event.action == "pause"
    )


def _event_key(user: str, public_thread: str) -> str:
    """传输层 key 必须带用户，避免多租户 thread 撞车。"""
    return f"{user}:{public_thread}"


def _event_is_terminal(payload: str) -> bool:
    """跨进程重放没有本地 StreamEntry，只能从事件载荷判断收尾。"""
    try:
        data = json.loads(payload)
    except ValueError:
        return False
    kind = data.get("type")
    if kind in {"run_finished", "run_failed", "approval_requested"}:
        return True
    return kind == "steering_applied" and data.get("action") == "pause"


def create_app(
    settings: Settings | None = None,
    auth: AuthConfig | None = None,
    *,
    hsts: bool = False,
    allow_write: bool = False,
    multi_user: bool | None = None,
    default_access_mode: str | None = None,
    stream_buffer: bool = False,
    desktop_mode: bool = False,
    client_source: str = "web",
    event_transport: Any | None = None,
    agent_client: Any | None = None,
) -> FastAPI:
    settings = apply_security_profile(settings or get_settings())
    # provider 档案是叠加在环境配置之上的；保留环境基线用于删除档案后回退。
    base_llm_providers = str(settings.llm_providers or "")
    auth = auth or AuthConfig.from_settings(settings)
    app = FastAPI(title="CodingAgent", docs_url=None, redoc_url=None)

    # 认证开启 ≠ 多用户。桌面壳有 token，但仍是单用户本地模式。
    is_multi_user = auth.enabled if multi_user is None else bool(multi_user)
    default_access = normalize_access_mode(default_access_mode)
    app.state.is_multi_user = is_multi_user

    # 单进程内所有租户共享一套资源池。
    governor = ConcurrencyGovernor(settings)
    tenants = TenantManager(
        settings, governor=governor, multi_user=is_multi_user
    )

    # A2 控制面：仅当配置了数据库时启用；默认仍走 gui-sessions.json。
    # 数据库不可用时进入只读降级，不能让整个控制面起不来。
    control_store = None
    control_db_url = str(settings.control_db_url or "").strip()
    control_db_status: dict[str, Any] = {"state": "disabled"}
    if control_db_url:
        from coding_agent.control.store import ControlStore

        try:
            control_store = ControlStore(
                control_db_url,
                auto_create=bool(settings.control_db_auto_create),
            )
            control_db_status = {"state": "ok"}
        except Exception as exc:  # noqa: BLE001 - 降级是契约行为
            control_db_status = {
                "state": "degraded",
                "error": f"{type(exc).__name__}: {exc}",
            }
            _LOG.warning(
                "控制面数据库不可用，进入只读降级：%s", exc
            )

    def _mark_db_degraded(exc: Exception) -> None:
        app.state.control_db_status = {
            "state": "degraded",
            "error": f"{type(exc).__name__}: {exc}",
        }

    def _control_db_degraded() -> bool:
        return bool(control_db_url) and (
            control_store is None
            or app.state.control_db_status.get("state") == "degraded"
        )

    def _reject_if_db_degraded() -> None:
        if _control_db_degraded():
            raise HTTPException(
                status_code=503,
                detail=(
                    "控制面数据库不可用；当前为只读降级模式，"
                    "请恢复数据库后重启控制面"
                ),
            )

    owns_transport = event_transport is None
    transport = event_transport
    if stream_buffer and transport is None:
        from coding_agent.event_transport import build_event_transport

        transport = build_event_transport(settings)

    # A2 → A3：配置了内部 URL 时，运行生命周期走独立 Agent Server。
    if agent_client is None and str(settings.agent_internal_url or "").strip():
        from coding_agent.control.agent_client import HttpAgentClient

        agent_client = HttpAgentClient(
            settings.agent_internal_url,
            settings.agent_internal_token,
        )

    runtimes = LocalRuntimeRegistry(
        allow_write=allow_write,
        governor=governor,
        default_access=default_access,
        approval_mode=WEB_APPROVAL_MODE,
        max_runtimes=settings.web_max_runtimes or 1,
        max_per_user=settings.web_max_runtimes_per_user or 1,
        runtime_ttl=settings.web_runtime_ttl_s or 0,
        secret_store_for=lambda tenant: _tenant_store(tenant),
    )
    streams: dict[tuple[str, str], StreamEntry] = {}
    session_configs: dict[tuple[str, str], SessionConfig] = {}
    # 删除是 tombstone：不再出现在列表里，但审计/checkpoint 保留供追责。
    deleted_sessions: set[tuple[str, str]] = set()
    sessions_loaded: set[str] = set()
    registry_lock = threading.Lock()
    session_file_lock = threading.Lock()
    limiter = FixedWindowRateLimiter(per_minute=settings.web_rate_limit_per_min)

    def _tenant(user: AuthUser) -> WebTenant:
        try:
            tenant = tenants.for_user(user.name)
        except TenantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 沙箱/目录不可用
            raise HTTPException(
                status_code=503,
                detail=f"工作区不可用：{type(exc).__name__}: {exc}",
            ) from exc
        _apply_provider_profiles()
        return tenant

    def _profiles_path() -> Path:
        return Path(settings.resolved_audit_dir).parent / (
            "provider-profiles.json"
        )

    def _apply_provider_profiles() -> list:
        """把 GUI 档案合并进 llm_providers；没有档案时恢复环境基线。

        档案只在单用户/桌面模式启用：多用户下共享 base_url 会让一个用户
        有机会把另一个用户的密钥发到自己的端点。
        """
        if is_multi_user:
            return []
        store = ProviderProfileStore(_profiles_path())
        profiles = store.load()
        if profiles:
            items = merge_provider_items(base_llm_providers, profiles)
            settings.llm_providers = json.dumps(
                items, ensure_ascii=False
            )
        elif settings.llm_providers != base_llm_providers:
            settings.llm_providers = base_llm_providers
        return profiles

    def _user_store(user: AuthUser) -> SecretStore:
        return (
            SecretStore(f"coding-agent:{user.name}")
            if is_multi_user
            else SecretStore()
        )

    def _tenant_store(tenant: WebTenant) -> SecretStore:
        return (
            SecretStore(f"coding-agent:{tenant.user}")
            if is_multi_user
            else SecretStore()
        )

    def _rate_limit(user: AuthUser) -> None:
        if limiter.allow(user.name):
            return
        retry = limiter.retry_after(user.name)
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁",
            headers={"Retry-After": str(retry)},
        )

    async def _sweep_expired() -> None:
        now = time.monotonic()
        # 事件缓冲只用于重连，保留 5 分钟；比 runtime TTL 短，避免长跑服务的
        # 内存被已完成会话无限占用。
        stream_ttl = 300.0
        with registry_lock:
            for key, stream_entry in list(streams.items()):
                if stream_entry.done and now - stream_entry.updated > stream_ttl:
                    streams.pop(key, None)
        await runtimes.sweep(now=now)

    def _effective_access(config: SessionConfig | None) -> str | None:
        return runtimes.effective_access(config)

    def _new_runtime(
        tenant: WebTenant, config: SessionConfig | None = None
    ) -> Any:
        return runtimes.new(tenant, config)

    def _ops_runtime(
        tenant: WebTenant, config: SessionConfig | None
    ) -> Any:
        return runtimes.ops(tenant, config)

    async def _drop_runtime(key: tuple[str, str], runtime: Any) -> None:
        await runtimes.drop(key, runtime)

    def _append_stream_event(entry: StreamEntry, event: Event) -> None:
        entry.seq += 1
        entry.events.append((entry.seq, event))
        entry.chars += len(event.model_dump_json())
        while entry.chars > _STREAM_BUFFER_MAX_CHARS and len(entry.events) > 1:
            _, old = entry.events.pop(0)
            entry.chars -= len(old.model_dump_json())
        if len(entry.events) > 5_000:
            dropped = entry.events[: len(entry.events) - 5_000]
            del entry.events[: len(entry.events) - 5_000]
            for _, old in dropped:
                entry.chars -= len(old.model_dump_json())
        entry.updated = time.monotonic()
        if transport is not None:
            try:
                transport.publish(
                    _event_key(entry.key[0], entry.public_thread),
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

    async def _consume_stream(
        entry: StreamEntry, events: AsyncIterator[Event]
    ) -> None:
        try:
            async for event in events:
                if _is_suspended(event):
                    entry.suspended = True
                _append_stream_event(entry, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 错误以事件形式送达订阅者
            failed = RunFailed(message=f"{type(exc).__name__}: {exc}")
            _append_stream_event(entry, failed)
        finally:
            if not entry.suspended:
                await _drop_runtime(entry.key, entry.runtime)
            entry.done = True
            entry.updated = time.monotonic()
            for queue in list(entry.subscribers):
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def _stream_guard(
        key: tuple[str, str], *, require_suspended: bool = False
    ) -> None:
        """拒绝在 active 流上再启动 resume；跨进程 resume 没有流时放行。"""
        old = streams.get(key)
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

    def _start_stream(
        key: tuple[str, str],
        public_thread: str,
        runtime: Any,
        events: AsyncIterator[Event],
    ) -> StreamEntry:
        old = streams.pop(key, None)
        # 序号跨 resume/进程重启保持单调；传输层可能保留旧进程的事件。
        base_seq = old.seq if old is not None else 0
        if transport is not None:
            try:
                base_seq = max(
                    base_seq,
                    transport.latest_seq(_event_key(key[0], public_thread)),
                )
            except Exception:  # noqa: BLE001
                _LOG.warning("读取事件传输序号失败", exc_info=True)
        # 每个用户只保留最近 8 个已完成流，避免长跑服务被重放缓冲撑爆。
        with registry_lock:
            owned = [
                (other_key, other)
                for other_key, other in streams.items()
                if other_key[0] == key[0] and other.done
            ]
            if len(owned) >= 8:
                owned.sort(key=lambda item: item[1].updated)
                for other_key, _ in owned[: len(owned) - 7]:
                    streams.pop(other_key, None)
        entry = StreamEntry(
            key=key,
            public_thread=public_thread,
            runtime=runtime,
            seq=base_seq,
        )
        entry.task = asyncio.create_task(_consume_stream(entry, events))
        streams[key] = entry
        return entry

    def _buffered_sse(
        entry: StreamEntry, *, after: int = 0
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last = max(0, int(after))
            queue: asyncio.Queue = asyncio.Queue(maxsize=2_000)
            entry.subscribers.append(queue)
            try:
                while True:
                    for seq, event in list(entry.events):
                        if seq <= last:
                            continue
                        last = seq
                        yield _sse_with_id(event, seq)
                    if entry.done and last >= entry.seq:
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        # 先把 done 前最后一批事件发完，再退出。
                        for seq, event in list(entry.events):
                            if seq > last:
                                last = seq
                                yield _sse_with_id(event, seq)
                        break
                    seq, event = item
                    if seq <= last:
                        continue
                    last = seq
                    yield _sse_with_id(event, seq)
            finally:
                try:
                    entry.subscribers.remove(queue)
                except ValueError:
                    pass

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Thread-Id": entry.public_thread,
            },
        )

    def _transport_sse(
        event_key: str, public_thread: str, *, after: int = 0
    ) -> StreamingResponse:
        """没有本地 StreamEntry 时，从共享传输按 seq 重放并继续轮询。"""

        async def stream() -> AsyncIterator[str]:
            last = max(0, int(after))
            idle = 0.0
            silent = 0.0
            while transport is not None:
                try:
                    rows = await asyncio.to_thread(
                        transport.replay, event_key, last
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
                    if _event_is_terminal(payload):
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
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Thread-Id": public_thread,
            },
        )

    def _get_entry(user: str, public_thread: str) -> RuntimeEntry | None:
        with registry_lock:
            entry = runtimes.get((user, public_thread))
        if entry is not None:
            entry.last_active = time.monotonic()
        return entry

    def _session_file(tenant: WebTenant) -> Path:
        return Path(tenant.settings.resolved_audit_dir) / "gui-sessions.json"

    def _read_legacy_rows(path: Path) -> list[dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [
            row for row in payload if isinstance(row, dict)
        ] if isinstance(payload, list) else []

    def _install_session_rows(
        user: AuthUser,
        tenant: WebTenant,
        rows: list[dict[str, Any]],
        deleted: set[str] | None = None,
    ) -> None:
        with registry_lock:
            for thread in deleted or ():
                deleted_sessions.add((user.name, thread))
            for row in rows:
                thread = str(row.get("thread_id") or "")
                if not thread:
                    continue
                if row.get("deleted"):
                    deleted_sessions.add((user.name, thread))
                    continue
                session_configs.setdefault(
                    (user.name, thread),
                    SessionConfig(
                        thread_id=thread,
                        workspace=str(
                            row.get("workspace") or tenant.workspace
                        ),
                        access_mode=normalize_access_mode(
                            row.get("access_mode")
                        ),
                        model=str(row.get("model") or ""),
                        provider=str(row.get("provider") or ""),
                        uploaded_bytes=int(row.get("uploaded_bytes") or 0),
                        title=str(row.get("title") or ""),
                    ),
                )

    def _load_session_configs(user: AuthUser, tenant: WebTenant) -> None:
        """按租户懒加载会话配置；进程重启后仍能恢复 workspace/权限/模型。

        配置了 A2 数据库时以数据库为准，并一次性导入旧
        `gui-sessions.json`；未配置时保持原有文件路径。
        """
        with registry_lock:
            if user.name in sessions_loaded:
                return
            sessions_loaded.add(user.name)
        path = _session_file(tenant)
        legacy = _read_legacy_rows(path)
        if control_store is not None and not _control_db_degraded():
            try:
                active, tombstones = control_store.load_sessions(user.name)
                if not active and not tombstones and legacy:
                    control_store.save_sessions(
                        user.name,
                        [row for row in legacy if not row.get("deleted")],
                        {
                            str(row.get("thread_id"))
                            for row in legacy
                            if row.get("deleted") and row.get("thread_id")
                        },
                    )
                    active, tombstones = control_store.load_sessions(
                        user.name
                    )
                _install_session_rows(
                    user, tenant, active, tombstones
                )
                return
            except Exception as exc:  # noqa: BLE001 - 降级为本地文件
                _mark_db_degraded(exc)
                _LOG.warning(
                    "控制面数据库读取失败，改用本地会话文件",
                    exc_info=True,
                )
        _install_session_rows(user, tenant, legacy)

    def _persist_session_configs(
        user: AuthUser, tenant: WebTenant
    ) -> None:
        with registry_lock:
            rows = [
                {
                    "thread_id": config.thread_id,
                    "workspace": config.workspace,
                    "access_mode": config.access_mode,
                    "model": config.model,
                    "provider": config.provider,
                    "uploaded_bytes": config.uploaded_bytes,
                    "title": config.title,
                }
                for (owner, _), config in session_configs.items()
                if owner == user.name
            ]
            tombstones = {
                thread
                for owner, thread in deleted_sessions
                if owner == user.name
            }
        if control_store is not None:
            if app.state.control_db_status.get("state") == "degraded":
                return
            try:
                control_store.save_sessions(
                    user.name, rows, tombstones
                )
            except Exception as exc:  # noqa: BLE001 - 运行中故障也要可预期
                _mark_db_degraded(exc)
                _LOG.warning(
                    "控制面数据库写入失败，进入只读降级", exc_info=True
                )
            return
        if control_db_url:
            # 启动即降级：不写 JSON，避免与恢复后的数据库分叉。
            return
        rows.extend(
            {"thread_id": thread, "deleted": True}
            for thread in tombstones
        )
        path = _session_file(tenant)
        with session_file_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(temp, path)
            except OSError:
                # 配置持久化失败不应让会话本身失败；内存态仍然可用。
                pass

    def _audit_workspace(
        user: AuthUser, tenant: WebTenant, public_thread: str
    ) -> str:
        """没有 GUI 配置时，从审计记录恢复该 thread 原来的工作区。"""
        try:
            info = SessionIndex(tenant.settings.resolved_audit_dir).get(
                tenant.thread_id(public_thread)
            )
        except Exception:  # noqa: BLE001 - 审计不可读时退回默认工作区
            return ""
        if info is None or not info.workspace:
            return ""
        try:
            return tenants.resolve_workspace(user.name, info.workspace)
        except Exception:  # noqa: BLE001
            return ""

    def _config_for(
        user: AuthUser,
        tenant: WebTenant,
        public_thread: str,
        *,
        create: bool = True,
    ) -> SessionConfig | None:
        _load_session_configs(user, tenant)
        key = (user.name, public_thread)
        with registry_lock:
            config = session_configs.get(key)
        if config is not None or not create:
            return config
        workspace = _audit_workspace(user, tenant, public_thread) or tenant.workspace
        config = SessionConfig(
            thread_id=public_thread,
            workspace=workspace,
            access_mode=default_access,
        )
        with registry_lock:
            session_configs.setdefault(key, config)
            result = session_configs[key]
        _persist_session_configs(user, tenant)
        return result

    def _set_session_config(
        user: AuthUser,
        tenant: WebTenant,
        config: SessionConfig,
    ) -> None:
        with registry_lock:
            session_configs[(user.name, config.thread_id)] = config
        _persist_session_configs(user, tenant)

    def _is_deleted(user: AuthUser, public_thread: str) -> bool:
        _load_session_configs(user, _tenant(user))
        with registry_lock:
            return (user.name, public_thread) in deleted_sessions

    def _reject_if_deleted(user: AuthUser, public_thread: str) -> None:
        if _is_deleted(user, public_thread):
            raise HTTPException(status_code=410, detail="会话已删除")

    def _agent_keys(
        user: AuthUser, tenant: WebTenant
    ) -> dict[str, str]:
        """把 A2 持有的 provider key 短期带给 A3；不写日志/数据库。"""
        try:
            store = _tenant_store(tenant)
            keys: dict[str, str] = {}
            for row in _provider_rows(tenant.settings, store):
                name = str(row.get("name") or "")
                if not name:
                    continue
                key = store.get(name)
                if (
                    not key
                    and name == "deepseek"
                    and not str(
                        tenant.settings.llm_providers or ""
                    ).strip()
                ):
                    key = tenant.settings.deepseek_api_key
                if key:
                    keys[name] = key
            return keys
        except Exception:  # noqa: BLE001 - 密钥不可用时由 A3 报缺 key
            _LOG.warning("读取 A2 provider key 失败", exc_info=True)
            return {}

    def _agent_context(
        user: AuthUser,
        tenant: WebTenant,
        config: SessionConfig | None,
        *,
        include_keys: bool = False,
    ) -> AgentContext:
        return AgentContext(
            tenant=tenant.user,
            user=user.name,
            workspace=(
                config.workspace if config else tenant.workspace
            ),
            access_mode=_effective_access(config),
            model=config.model if config else "",
            provider=config.provider if config else "",
            allow_write=allow_write,
            api_keys=(
                _agent_keys(user, tenant) if include_keys else {}
            ),
        )

    async def _proxy_agent(
        stream: Any, *, public_thread: str = ""
    ) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            async for chunk in stream.aiter_bytes():
                yield chunk

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        if public_thread:
            headers["X-Thread-Id"] = public_thread
        return StreamingResponse(
            body(),
            status_code=int(stream.status_code),
            media_type="text/event-stream",
            headers=headers,
        )

    def _agent_failure(exc: Exception) -> HTTPException:
        if isinstance(exc, AgentClientError):
            return HTTPException(status_code=exc.status_code, detail=exc.detail)
        return HTTPException(
            status_code=502,
            detail=f"Agent Server 不可用：{type(exc).__name__}: {exc}",
        )

    async def _agent_json(
        user: AuthUser,
        tenant: WebTenant,
        config: SessionConfig | None,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        include_keys: bool = False,
    ) -> dict:
        try:
            return await agent_client.json_request(
                _agent_context(
                    user, tenant, config, include_keys=include_keys
                ),
                method,
                path,
                params=params,
                json=json_body,
            )
        except Exception as exc:  # noqa: BLE001
            raise _agent_failure(exc) from exc

    async def _agent_upload(
        user: AuthUser,
        tenant: WebTenant,
        config: SessionConfig | None,
        thread_id: str,
        files: list[tuple[str, bytes, str]],
    ) -> dict:
        try:
            return await agent_client.upload_files(
                _agent_context(user, tenant, config),
                thread_id,
                files,
            )
        except Exception as exc:  # noqa: BLE001
            raise _agent_failure(exc) from exc

    def _workspace_uploaded_bytes(
        user: AuthUser, workspace: str
    ) -> int:
        """同一工作区跨会话的上传总量，防止新建会话重置配额。"""
        with registry_lock:
            return sum(
                config.uploaded_bytes
                for (owner, _), config in session_configs.items()
                if owner == user.name and config.workspace == workspace
            )

    def _provider_rows(
        cfg: Settings, store: SecretStore
    ) -> list[dict[str, Any]]:
        """列出 provider/model；绝不返回密钥明文。"""
        raw = str(cfg.llm_providers or "").strip()
        if not raw:
            key = bool(cfg.deepseek_api_key) or store.has("deepseek")
            return [
                {
                    "name": "deepseek",
                    "model": cfg.deepseek_model,
                    "base_url": cfg.deepseek_base_url,
                    "has_key": key,
                    "capabilities": [],
                    "task_types": [],
                }
            ]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        items = parsed if isinstance(parsed, list) else [parsed]
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            name = str(item.get("name") or f"provider-{index}")
            key = bool(item.get("api_key")) or bool(
                item.get("api_key_env") and os.environ.get(
                    str(item.get("api_key_env")), ""
                )
            ) or store.has(name)
            rows.append(
                {
                    "name": name,
                    "model": str(
                        item.get("model") or cfg.deepseek_model
                    ),
                    "base_url": str(
                        item.get("base_url") or cfg.deepseek_base_url
                    ),
                    "has_key": key,
                    "capabilities": [
                        str(value)
                        for value in (item.get("capabilities") or [])
                    ],
                    "task_types": [
                        str(value)
                        for value in (item.get("task_types") or [])
                    ],
                }
            )
        return rows

    def _validate_provider_choice(
        cfg: Settings, store: SecretStore, provider: str
    ) -> None:
        if not provider:
            return
        names = {row["name"] for row in _provider_rows(cfg, store)}
        if provider not in names:
            raise HTTPException(
                status_code=422,
                detail=f"未知或未启用的 provider：{provider}",
            )

    def _resolve_provider(
        cfg: Settings, store: SecretStore, name: str
    ) -> dict[str, str] | None:
        raw = str(cfg.llm_providers or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None
            items = parsed if isinstance(parsed, list) else [parsed]
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                if item.get("enabled", True) is False:
                    continue
                if str(item.get("name") or f"provider-{index}") != name:
                    continue
                api_key = str(item.get("api_key") or "")
                env_name = str(item.get("api_key_env") or "")
                if not api_key and env_name:
                    api_key = os.environ.get(env_name, "")
                if not api_key:
                    api_key = store.get(name)
                return {
                    "name": name,
                    "base_url": str(
                        item.get("base_url") or cfg.deepseek_base_url
                    ),
                    "model": str(item.get("model") or cfg.deepseek_model),
                    "api_key": api_key,
                }
            return None
        if name != "deepseek":
            return None
        return {
            "name": "deepseek",
            "base_url": cfg.deepseek_base_url,
            "model": cfg.deepseek_model,
            "api_key": store.get("deepseek") or cfg.deepseek_api_key,
        }

    def _capabilities(config: SessionConfig | None) -> dict[str, Any]:
        access = _effective_access(config)
        read_only = access == ACCESS_READ_ONLY
        return {
            "access_mode": access or "legacy",
            "read_only": read_only,
            "write_enabled": not read_only and bool(access),
            "execution_enabled": not read_only,
            "can_upload": True,
            "can_restore": True,
            "can_fork": True,
            "can_steer": True,
            "multi_user": is_multi_user,
            "server_write_enabled": allow_write,
            "security_profile": settings.security_profile,
            "workspace": config.workspace if config is not None else "",
            "run_cost_budget_usd": float(
                settings.run_cost_budget_usd or 0.0
            ),
            "cost_alert_ratio": float(settings.cost_alert_ratio or 0.8),
            "max_upload_bytes": int(settings.web_max_upload_bytes),
            "max_upload_total_bytes": int(
                settings.web_max_upload_total_bytes
            ),
        }

    async def _runtime_for_resume(
        user: AuthUser, public_thread: str
    ) -> tuple[tuple[str, str], Any]:
        key = (user.name, public_thread)
        entry = _get_entry(*key)
        if entry is not None:
            return key, entry.runtime
        # 进程重启/TTL 清理后，从 per-user checkpoint 重建。
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread)
        runtime = _new_runtime(tenant, config)
        with registry_lock:
            runtimes.put(key, runtime)
        return key, runtime

    def _sse_response(
        key: tuple[str, str],
        public_thread: str,
        runtime: Any,
        events: AsyncIterator[Event],
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            suspended = False
            try:
                async for event in events:
                    if _is_suspended(event):
                        suspended = True
                    yield _sse(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 错误以事件形式送到前端
                yield _sse(RunFailed(message=f"{type(exc).__name__}: {exc}"))
            finally:
                if suspended:
                    entry = _get_entry(*key)
                    if entry is not None:
                        entry.last_active = time.monotonic()
                else:
                    await _drop_runtime(key, runtime)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Thread-Id": public_thread,
            },
        )

    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.web_max_body_bytes,
        upload_max_bytes=settings.web_max_upload_bytes,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    app.state.auth = auth
    app.state.tenants = tenants
    app.state.governor = governor
    app.state.desktop_mode = desktop_mode
    app.state.control_store = control_store
    app.state.control_db_status = control_db_status
    app.state.event_transport = transport
    app.state.agent_client = agent_client
    # 独立进程模式下由 control_server 在启动时用握手结果覆盖。
    app.state.agent_status = {
        "state": "in_process" if agent_client is None else "unknown"
    }
    if agent_client is not None:
        from coding_agent.agent_protocol import check_compatibility

        async def _probe_agent_forever() -> None:
            while True:
                try:
                    meta = await agent_client.probe()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - A3 重启是预期状态
                    app.state.agent_status = {
                        "state": "unavailable",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                else:
                    compat = check_compatibility(meta)
                    app.state.agent_status = {
                        "state": "ok" if compat.ok else "incompatible",
                        "meta": meta.to_dict(),
                        "reason": compat.reason,
                        "warning": compat.warning,
                    }
                await asyncio.sleep(AGENT_PROBE_INTERVAL_S)

        async def _start_probe() -> None:
            app.state.agent_probe_task = asyncio.create_task(
                _probe_agent_forever(), name="agent-probe"
            )

        async def _stop_probe() -> None:
            task = getattr(app.state, "agent_probe_task", None)
            if task is None:
                return
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 关闭阶段不再抛错
                pass

        app.router.on_startup.append(_start_probe)
        app.router.on_shutdown.append(_stop_probe)
    if control_store is not None:
        # FastAPI 新版本移除了 add_event_handler；router 的 on_shutdown 仍可用。
        app.router.on_shutdown.append(control_store.close)
    if owns_transport and transport is not None:
        app.router.on_shutdown.append(transport.close)
    if agent_client is not None:
        app.router.on_shutdown.append(agent_client.aclose)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _reject_if_active(user: AuthUser, public_thread: str) -> None:
        if _get_entry(user.name, public_thread) is not None:
            raise HTTPException(
                status_code=409,
                detail="该会话正在运行或已挂起；请先结束/恢复再执行此操作",
            )

    def _config_and_tenant(
        user: AuthUser, public_thread: str
    ) -> tuple[WebTenant, SessionConfig | None]:
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        if config is None:
            workspace = _audit_workspace(user, tenant, public_thread)
            if workspace:
                config = SessionConfig(
                    thread_id=public_thread,
                    workspace=workspace,
                    access_mode=default_access,
                )
        return tenant, config

    register_system_routes(
        app,
        SystemDeps(
            settings=settings,
            tenants=tenants,
            tenant=_tenant,
            tenant_store=_tenant_store,
            provider_rows=_provider_rows,
            config_for=_config_for,
            capabilities=_capabilities,
            default_access=default_access,
            desktop_mode=desktop_mode,
        ),
    )

    register_provider_routes(
        app,
        ProviderDeps(
            settings=settings,
            allow_write=allow_write,
            is_multi_user=is_multi_user,
            base_llm_providers=base_llm_providers,
            profiles_path=_profiles_path,
            apply_profiles=_apply_provider_profiles,
            resolve_provider=_resolve_provider,
            user_store=_user_store,
            rate_limit=_rate_limit,
            provider_rows=_provider_rows,
        ),
    )

    register_session_routes(
        app,
        SessionDeps(
            tenant=_tenant,
            sweep_expired=_sweep_expired,
            load_configs=_load_session_configs,
            config_for=_config_for,
            set_config=_set_session_config,
            persist_configs=_persist_session_configs,
            effective_access=_effective_access,
            capabilities=_capabilities,
            reject_deleted=_reject_if_deleted,
            reject_active=_reject_if_active,
            reject_db_degraded=_reject_if_db_degraded,
            rate_limit=_rate_limit,
            resolve_workspace=lambda name, ws: tenants.resolve_workspace(
                name, ws
            ),
            validate_provider=lambda tenant, user, provider: (
                _validate_provider_choice(
                    tenant.settings, _user_store(user), provider
                )
            ),
            agent_json=_agent_json,
            ops_runtime=_ops_runtime,
            agent_client=agent_client,
            allow_write=allow_write,
            registry_lock=registry_lock,
            session_configs=session_configs,
            deleted_sessions=deleted_sessions,
            runtimes=runtimes,
        ),
    )

    register_workspace_routes(
        app,
        WorkspaceDeps(
            tenant=_tenant,
            rate_limit=_rate_limit,
            resolve_workspace=lambda name, ws: tenants.resolve_workspace(
                name, ws
            ),
            reject_deleted=_reject_if_deleted,
            config_for=_config_for,
            agent_json=_agent_json,
            agent_failure=_agent_failure,
            agent_client=agent_client,
            default_access=default_access,
            allow_write=allow_write,
            desktop_mode=desktop_mode,
        ),
    )

    register_upload_routes(
        app,
        UploadDeps(
            settings=settings,
            tenant=_tenant,
            rate_limit=_rate_limit,
            config_for=_config_for,
            set_config=_set_session_config,
            reject_active=_reject_if_active,
            reject_deleted=_reject_if_deleted,
            agent_client=agent_client,
            agent_upload=_agent_upload,
            agent_json=_agent_json,
            ops_runtime=_ops_runtime,
            workspace_uploaded_bytes=_workspace_uploaded_bytes,
        ),
    )

    register_ops_routes(
        app,
        OpsDeps(
            tenant=_tenant,
            sweep_expired=_sweep_expired,
            load_configs=_load_session_configs,
            config_for=_config_for,
            deleted_sessions=deleted_sessions,
            agent_client=agent_client,
            agent_json=_agent_json,
            ops_runtime=_ops_runtime,
            rate_limit=_rate_limit,
            get_entry=_get_entry,
            control_store=control_store,
        ),
    )

    register_snapshot_routes(
        app,
        SnapshotDeps(
            config_and_tenant=_config_and_tenant,
            reject_active=_reject_if_active,
            agent_client=agent_client,
            agent_json=_agent_json,
            ops_runtime=_ops_runtime,
            set_config=_set_session_config,
            effective_access=_effective_access,
        ),
    )

    register_run_routes(
        app,
        RunDeps(
            settings=settings,
            client_source=client_source,
            stream_buffer=stream_buffer,
            transport=transport,
            streams=streams,
            registry_lock=registry_lock,
            runtimes=runtimes,
            rate_limit=_rate_limit,
            sweep_expired=_sweep_expired,
            tenant=_tenant,
            config_for=_config_for,
            set_config=_set_session_config,
            reject_deleted=_reject_if_deleted,
            agent_client=agent_client,
            agent_context=_agent_context,
            agent_failure=_agent_failure,
            proxy_agent=_proxy_agent,
            stream_guard=_stream_guard,
            start_stream=_start_stream,
            buffered_sse=_buffered_sse,
            sse_response=_sse_response,
            get_entry=_get_entry,
            runtime_for_resume=_runtime_for_resume,
            new_runtime=_new_runtime,
            event_key=_event_key,
            transport_sse=_transport_sse,
            ops_runtime=_ops_runtime,
        ),
    )

    register_desktop_routes(
        app,
        DesktopDeps(
            app=app,
            desktop_mode=desktop_mode,
            registry_lock=registry_lock,
            runtimes=runtimes,
            tenants=tenants,
        ),
    )

    register_compact_routes(
        app,
        CompactDeps(
            config_and_tenant=_config_and_tenant,
            reject_active=_reject_if_active,
            agent_client=agent_client,
            agent_json=_agent_json,
            set_config=_set_session_config,
            effective_access=_effective_access,
            ops_runtime=_ops_runtime,
        ),
    )

    return app
