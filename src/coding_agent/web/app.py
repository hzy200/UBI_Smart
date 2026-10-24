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
import re
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from coding_agent.access import ACCESS_READ_ONLY, normalize_access_mode
from coding_agent.agent_protocol import AgentContext, AgentRunRequest
from coding_agent.audit import read_records_many
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
from coding_agent.project_env import probe_project
from coding_agent.provider_profiles import (
    ProviderProfileStore,
    merge_provider_items,
    profile_from_payload,
)
from coding_agent.secrets_store import SecretStore, SecretStoreUnavailable
from coding_agent.tenancy import TenantError, TenantManager, WebTenant
from coding_agent.web.auth import AuthConfig, AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.local_runtime import LocalRuntimeRegistry, RuntimeEntry
from coding_agent.web.middleware import (
    BodySizeLimitMiddleware,
    FixedWindowRateLimiter,
    SecurityHeadersMiddleware,
)
from coding_agent.web.routes.system import SystemDeps, register_system_routes
from coding_agent.web.validation import check_thread_id as _check_thread_id
from coding_agent.workspace.browse import (
    BrowseError,
    git_summary,
    list_directory,
    read_text_file,
)

STATIC_DIR = Path(__file__).parent / "static"
_LOG = logging.getLogger(__name__)

WEB_APPROVAL_MODE = "ask"
# 每个会话的重放缓冲上限；超出后丢最旧事件（序号仍连续）。
_STREAM_BUFFER_MAX_CHARS = 8 * 1024 * 1024
# A2 周期探测 A3；A3 重启后状态自动从 unavailable 回到 ok。
AGENT_PROBE_INTERVAL_S = 5.0


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    thread_id: str = Field(default="", max_length=64)
    # Web 刻意不提供写会话：所有写操作都要求本机 CLI/TUI 的显式 --write。
    # 旧版这里有一个 allow_write 字段，但从未传给 AgentRuntime，属于误导性 API。


class SteerRequest(BaseModel):
    action: Literal["pause", "cancel", "add", "replan"]
    text: str = Field(default="", max_length=4_000)
    role: str = "explore"


class ResumeRequest(BaseModel):
    decisions: dict[str, bool] = Field(default_factory=dict)


class SessionRequest(BaseModel):
    """新建/绑定一个会话。

    workspace 与 access_mode 在**服务端**绑定，之后不能由单次 run 请求改写。
    """

    thread_id: str = Field(default="", max_length=64)
    workspace: str = Field(default="", max_length=4096)
    access_mode: Literal["read_only", "write_ask", "write_auto"] = "read_only"
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=64)


class ModelRequest(BaseModel):
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=64)


class CompactRequest(BaseModel):
    keep_recent: int = Field(default=6, ge=1, le=50)


class RestoreRequest(BaseModel):
    thread_id: str
    path: str = ""
    snapshot_id: str = ""
    prefix: str = ""
    dry_run: bool = False


class RenameRequest(BaseModel):
    title: str = Field(default="", max_length=120)


class UploadDeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class ProviderKeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=8_000)


class ProviderTestRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class MemoryRequest(BaseModel):
    action: Literal["remember", "forget", "clear", "pin", "priority"]
    thread_id: str = ""
    text: str = Field(default="", max_length=4_000)
    index: int = -1
    scope: str = "project"
    pinned: bool = True
    priority: int = 0


@dataclass
class SessionConfig:
    thread_id: str
    workspace: str
    access_mode: str = ACCESS_READ_ONLY
    model: str = ""
    provider: str = ""
    uploaded_bytes: int = 0
    # GUI 重命名；空 = 使用首条提示词生成的标题。
    title: str = ""
    created_at: float = field(default_factory=time.monotonic)


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

    @app.post("/api/desktop/shutdown")
    async def desktop_shutdown(user: UserDep) -> dict:
        """桌面 server 的优雅退出入口；仅 desktop_mode 暴露。"""
        if not desktop_mode:
            raise HTTPException(status_code=404, detail="not found")
        callback = getattr(app.state, "shutdown", None)
        if callable(callback):
            asyncio.get_running_loop().call_soon(callback)
        return {"ok": True}

    @app.post("/api/desktop/cancel_all")
    async def desktop_cancel_all(user: UserDep) -> dict:
        """桌面窗口关闭前协作式取消所有运行；仅桌面模式暴露。"""
        if not desktop_mode:
            raise HTTPException(status_code=404, detail="not found")
        with registry_lock:
            entries = list(runtimes.items())
        cancelled = 0
        for (owner, public_thread), entry in entries:
            try:
                tenant = tenants.for_user(owner)
                entry.runtime.steer(
                    tenant.thread_id(public_thread),
                    "cancel",
                    source="desktop-close",
                )
                cancelled += 1
            except Exception:  # noqa: BLE001 - 单个会话取消失败不阻断关窗
                continue
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with registry_lock:
                active = any(
                    getattr(item.runtime, "_stream_active", False)
                    for item in runtimes.values()
                )
            if not active:
                break
            await asyncio.sleep(0.1)
        return {"cancelled": cancelled, "waited": not active}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/project")
    async def project(user: UserDep, workspace: str = "") -> dict:
        """只读探测项目解释器/包管理器；不创建环境、不执行项目代码。"""
        _rate_limit(user)
        tenant = _tenant(user)
        try:
            target = tenants.resolve_workspace(
                user.name, workspace or tenant.workspace
            )
        except (TenantError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if agent_client is not None:
            context = AgentContext(
                tenant=tenant.user,
                user=user.name,
                workspace=target,
                access_mode=default_access or ACCESS_READ_ONLY,
                allow_write=allow_write,
            )
            try:
                return await agent_client.json_request(
                    context, "GET", "/v1/project"
                )
            except Exception as exc:  # noqa: BLE001
                raise _agent_failure(exc) from exc
        result = await asyncio.to_thread(probe_project, target)
        result["desktop_mode"] = desktop_mode
        return result

    def _workspace_for(user: AuthUser, thread_id: str) -> str:
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        return config.workspace if config else tenant.workspace

    @app.get("/api/files")
    async def files(
        user: UserDep,
        thread_id: str,
        path: str = "",
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        tenant = _tenant(user)
        config = _config_for(
            user, tenant, public_thread, create=False
        )
        if agent_client is not None:
            return await _agent_json(
                user, tenant, config, "GET", "/v1/files",
                params={"path": path},
            )
        workspace = config.workspace if config else tenant.workspace
        try:
            return await asyncio.to_thread(list_directory, workspace, path)
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/file")
    async def file_content(
        user: UserDep,
        thread_id: str,
        path: str,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        tenant = _tenant(user)
        config = _config_for(
            user, tenant, public_thread, create=False
        )
        if agent_client is not None:
            return await _agent_json(
                user, tenant, config, "GET", "/v1/file",
                params={"path": path},
            )
        workspace = config.workspace if config else tenant.workspace
        try:
            return await asyncio.to_thread(
                read_text_file, workspace, path
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/git")
    async def git_panel(
        user: UserDep,
        thread_id: str,
        diff: bool = False,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        tenant = _tenant(user)
        config = _config_for(
            user, tenant, public_thread, create=False
        )
        if agent_client is not None:
            return await _agent_json(
                user, tenant, config, "GET", "/v1/git",
                params={"diff": "true" if diff else "false"},
            )
        workspace = config.workspace if config else tenant.workspace
        try:
            return await asyncio.to_thread(
                git_summary, workspace, diff=diff
            )
        except (BrowseError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

    @app.get("/api/sessions")
    async def sessions(
        user: UserDep,
        limit: int = 20,
        q: str = "",
    ) -> dict:
        await _sweep_expired()
        tenant = _tenant(user)
        if agent_client is not None:
            _load_session_configs(user, tenant)
            data = await _agent_json(
                user, tenant, None, "GET", "/v1/sessions",
                params={"limit": 500, "q": q},
            )
            rows = []
            seen: set[str] = set()
            for item in data.get("sessions", []):
                thread = str(item.get("thread_id") or "")
                if not thread:
                    continue
                if (user.name, thread) in deleted_sessions:
                    continue
                seen.add(thread)
                config = _config_for(
                    user, tenant, thread, create=False
                )
                rows.append(
                    {
                        "thread_id": thread,
                        "last_active": item.get("last_active", ""),
                        "prompts": item.get("prompts", 0),
                        "tool_calls": item.get("tool_calls", 0),
                        "title": (
                            (config.title if config else "")
                            or item.get("title", "")
                        ),
                        "workspace": (
                            config.workspace
                            if config
                            else item.get("workspace", "")
                        ),
                        "access_mode": _effective_access(config),
                        "model": config.model if config else "",
                        "provider": config.provider if config else "",
                        "run_count": item.get("runs", 0),
                        "interrupted": bool(item.get("interrupted")),
                    }
                )
            with registry_lock:
                pending = [
                    config
                    for (owner, _), config in session_configs.items()
                    if owner == user.name
                    and config.thread_id not in seen
                    and (owner, config.thread_id) not in deleted_sessions
                ]
            for config in pending:
                rows.append(
                    {
                        "thread_id": config.thread_id,
                        "last_active": "",
                        "prompts": 0,
                        "tool_calls": 0,
                        "title": config.title or "（未运行）",
                        "workspace": config.workspace,
                        "access_mode": _effective_access(config),
                        "model": config.model,
                        "provider": config.provider,
                        "run_count": 0,
                        "interrupted": False,
                    }
                )
            query = q.strip().lower()
            if query:
                rows = [
                    row
                    for row in rows
                    if query in str(row.get("title", "")).lower()
                    or query in row["thread_id"].lower()
                    or query in str(row.get("workspace", "")).lower()
                ]
            return {"sessions": rows[: max(1, limit)]}
        index_ = SessionIndex(tenant.settings.resolved_audit_dir)
        found = index_.list(limit=0)
        rows = []
        seen: set[str] = set()
        for info in found:
            public_thread = tenant.public_thread(info.thread_id)
            if (user.name, public_thread) in deleted_sessions:
                continue
            seen.add(public_thread)
            config = _config_for(
                user, tenant, public_thread, create=False
            )
            rows.append(
                {
                    "thread_id": public_thread,
                    "last_active": info.last_seen,
                    "prompts": info.prompts,
                    "tool_calls": info.tool_calls,
                    "title": (config.title if config else "") or info.title,
                    "workspace": (
                        config.workspace if config else info.workspace
                    ),
                    "access_mode": _effective_access(config),
                    "model": config.model if config else "",
                    "provider": config.provider if config else "",
                    "run_count": info.runs,
                    "interrupted": bool(info.interrupted),
                }
            )
        # 创建但尚未运行（或 fork/compact 后尚未运行）的会话也要可见，
        # 否则重启后侧栏会“丢”掉这些 thread。
        _load_session_configs(user, tenant)
        with registry_lock:
            pending = [
                config
                for (owner, _), config in session_configs.items()
                if owner == user.name
                and config.thread_id not in seen
                and (owner, config.thread_id) not in deleted_sessions
            ]
        for config in pending:
            rows.append(
                {
                    "thread_id": config.thread_id,
                    "last_active": "",
                    "prompts": 0,
                    "tool_calls": 0,
                    "title": config.title or "（未运行）",
                    "workspace": config.workspace,
                    "access_mode": _effective_access(config),
                    "model": config.model,
                    "provider": config.provider,
                    "run_count": 0,
                    "interrupted": False,
                }
            )
        query = q.strip().lower()
        if query:
            rows = [
                row
                for row in rows
                if query in str(row.get("title", "")).lower()
                or query in row["thread_id"].lower()
                or query in str(row.get("workspace", "")).lower()
            ]
        return {"sessions": rows[: max(1, limit)]}

    @app.post("/api/sessions")
    async def create_session(
        request: SessionRequest,
        user: UserDep,
    ) -> dict:
        """绑定会话的 workspace / 权限 / 模型；这些字段不能由单次 run 改写。"""
        _rate_limit(user)
        _reject_if_db_degraded()
        public_thread = _check_thread_id(request.thread_id)
        key = (user.name, public_thread)
        with registry_lock:
            if runtimes.contains(key):
                raise HTTPException(
                    status_code=409,
                    detail="该会话正在运行或已挂起；请先结束/恢复",
                )
        _reject_if_deleted(user, public_thread)
        existing = _config_for(
            user, _tenant(user), public_thread, create=False
        )
        if existing is not None:
            return {
                "thread_id": public_thread,
                "workspace": existing.workspace,
                "access_mode": _effective_access(existing),
                "model": existing.model,
                "provider": existing.provider,
                "capabilities": _capabilities(existing),
            }

        tenant = _tenant(user)
        try:
            workspace = tenants.resolve_workspace(
                user.name, request.workspace
            )
        except (TenantError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        access_mode = normalize_access_mode(request.access_mode) or ACCESS_READ_ONLY
        if access_mode != ACCESS_READ_ONLY and not allow_write:
            raise HTTPException(
                status_code=403,
                detail="服务端未开启写能力；请以 --write 启动或选择只读模式",
            )
        _validate_provider_choice(
            tenant.settings, _user_store(user), request.provider
        )
        config = SessionConfig(
            thread_id=public_thread,
            workspace=workspace,
            access_mode=access_mode,
            model=request.model,
            provider=request.provider,
        )
        _set_session_config(user, tenant, config)
        return {
            "thread_id": public_thread,
            "workspace": workspace,
            "access_mode": access_mode,
            "model": config.model,
            "provider": config.provider,
            "capabilities": _capabilities(config),
        }

    @app.post("/api/sessions/{thread_id}/model")
    async def switch_model(
        thread_id: str,
        request: ModelRequest,
        user: UserDep,
    ) -> dict:
        """空闲时切换模型；thread/checkpoint 保留，下一次 run 重建 runtime。"""
        _reject_if_db_degraded()
        public_thread = _check_thread_id(thread_id)
        _reject_if_active(user, public_thread)
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        _validate_provider_choice(
            tenant.settings, _user_store(user), request.provider
        )
        config.model = request.model
        config.provider = request.provider
        _set_session_config(user, tenant, config)
        return {
            "ok": True,
            "thread_id": public_thread,
            "model": config.model,
            "provider": config.provider,
        }

    @app.post("/api/sessions/{thread_id}/rename")
    async def rename_session(
        thread_id: str,
        request: RenameRequest,
        user: UserDep,
    ) -> dict:
        _reject_if_db_degraded()
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        config.title = " ".join(request.title.split())[:120]
        _set_session_config(user, tenant, config)
        return {
            "ok": True,
            "thread_id": public_thread,
            "title": config.title,
        }

    @app.delete("/api/sessions/{thread_id}")
    async def delete_session(thread_id: str, user: UserDep) -> dict:
        """Tombstone 删除：列表隐藏并删除会话配置，审计/checkpoint 保留。"""
        _reject_if_db_degraded()
        public_thread = _check_thread_id(thread_id)
        _reject_if_active(user, public_thread)
        tenant = _tenant(user)
        _load_session_configs(user, tenant)
        with registry_lock:
            deleted_sessions.add((user.name, public_thread))
            session_configs.pop((user.name, public_thread), None)
        _persist_session_configs(user, tenant)
        return {"ok": True, "thread_id": public_thread}

    @app.get("/api/sessions/{thread_id}/export")
    async def export_session(
        thread_id: str,
        user: UserDep,
        format: Literal["markdown", "json"] = "markdown",
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        if agent_client is not None:
            data = await _agent_json(
                user, tenant, config, "GET",
                f"/v1/history/{public_thread}",
            )
            messages = [
                {
                    "role": str(item.get("role") or ""),
                    "text": str(item.get("text") or ""),
                }
                for item in data.get("messages", [])
            ]
        else:
            runtime = _ops_runtime(tenant, config)
            try:
                history = await runtime.history(
                    tenant.thread_id(public_thread), limit=None
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc
            finally:
                await runtime.aclose()
            messages = [
                {"role": message.role, "text": message.text}
                for message in history
            ]
        title = (
            (config.title if config else "")
            or next(
                (
                    item["text"]
                    for item in messages
                    if item["role"] == "user"
                ),
                public_thread,
            )
        ).strip()
        if format == "json":
            return {
                "format": "json",
                "thread_id": public_thread,
                "title": title,
                "workspace": config.workspace if config else tenant.workspace,
                "messages": messages,
            }
        lines = [
            f"# {title}",
            "",
            f"- thread: {public_thread}",
            f"- workspace: {config.workspace if config else tenant.workspace}",
            "",
        ]
        for message in messages:
            role = "用户" if message["role"] == "user" else "助手"
            lines.extend([f"## {role}", "", message["text"], ""])
        return {
            "format": "markdown",
            "thread_id": public_thread,
            "title": title,
            "content": "\n".join(lines).rstrip() + "\n",
        }

    @app.post("/api/context/{thread_id}/compact")
    async def compact_context(
        thread_id: str,
        request: CompactRequest,
        user: UserDep,
    ) -> dict:
        """压缩并分叉；原 thread 保留，不原地删消息。"""
        public_thread = _check_thread_id(thread_id)
        _reject_if_active(user, public_thread)
        tenant, config = _config_and_tenant(user, public_thread)
        if agent_client is not None:
            result = await _agent_json(
                user, tenant, config, "POST",
                f"/v1/compact/{public_thread}",
                json_body={"keep_recent": request.keep_recent},
            )
            target_public = str(result.get("thread_id") or "")
            if target_public:
                _set_session_config(
                    user,
                    tenant,
                    SessionConfig(
                        thread_id=target_public,
                        workspace=(
                            config.workspace
                            if config
                            else tenant.workspace
                        ),
                        access_mode=_effective_access(config),
                        model=config.model if config else "",
                        provider=config.provider if config else "",
                    ),
                )
            return {
                "thread_id": target_public,
                "source_thread_id": public_thread,
            }
        runtime = _ops_runtime(tenant, config)
        try:
            target_internal = await runtime.compact_thread(
                tenant.thread_id(public_thread),
                keep_recent=request.keep_recent,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=409,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        target_public = tenant.public_thread(target_internal)
        _set_session_config(
            user,
            tenant,
            SessionConfig(
                thread_id=target_public,
                workspace=config.workspace if config else tenant.workspace,
                access_mode=_effective_access(config),
                model=config.model if config else "",
                provider=config.provider if config else "",
            ),
        )
        return {"thread_id": target_public, "source_thread_id": public_thread}

    @app.post("/api/sessions/{thread_id}/uploads")
    async def upload_files(
        thread_id: str,
        user: UserDep,
        files: Annotated[list[UploadFile], File()],
    ) -> dict:
        """上传写入 `.agent/uploads`，不授予模型修改源码的能力。"""
        _rate_limit(user)
        public_thread = _check_thread_id(thread_id)
        _reject_if_active(user, public_thread)
        if len(files) > max(1, int(settings.web_max_upload_files)):
            raise HTTPException(
                status_code=413,
                detail=f"单次最多上传 {settings.web_max_upload_files} 个文件",
            )
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if agent_client is not None:
            payload: list[tuple[str, bytes, str]] = []
            total = 0
            for item in files:
                data = await item.read()
                if len(data) > settings.web_max_upload_bytes:
                    raise HTTPException(
                        status_code=413, detail="单个文件超过上传上限"
                    )
                total += len(data)
                payload.append(
                    (
                        item.filename or "upload.bin",
                        data,
                        item.content_type or "application/octet-stream",
                    )
                )
            current = _workspace_uploaded_bytes(user, config.workspace)
            if current + total > settings.web_max_upload_total_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "工作区累计上传超过上限 "
                        f"{settings.web_max_upload_total_bytes} 字节"
                    ),
                )
            result = await _agent_upload(
                user, tenant, config, public_thread, payload
            )
            added = sum(
                int(item.get("bytes", 0))
                for item in result.get("uploads", [])
            )
            config.uploaded_bytes += added
            _set_session_config(user, tenant, config)
            return result
        runtime = _ops_runtime(tenant, config)
        internal = tenant.thread_id(public_thread)
        uploaded = []
        before_bytes = config.uploaded_bytes
        total_limit = settings.web_max_upload_total_bytes
        # 已知大小时先整批预检，避免写到一半才因配额失败。
        known_sizes: list[int] | None = []
        for item in files:
            size = getattr(item, "size", None)
            if size is None:
                known_sizes = None
                break
            if size > settings.web_max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"{item.filename} 超过上限 "
                        f"{settings.web_max_upload_bytes} 字节"
                    ),
                )
            known_sizes.append(int(size))
        if known_sizes is not None:
            current = _workspace_uploaded_bytes(user, config.workspace)
            if current + sum(known_sizes) > total_limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"工作区累计上传超过上限 {total_limit} 字节",
                )
        try:
            for item in files:
                data = await item.read()
                if len(data) > settings.web_max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"{item.filename} 超过上限 "
                            f"{settings.web_max_upload_bytes} 字节"
                        ),
                    )
                if (
                    _workspace_uploaded_bytes(user, config.workspace)
                    + len(data)
                    > total_limit
                ):
                    raise HTTPException(
                        status_code=413,
                        detail=f"工作区累计上传超过上限 {total_limit} 字节",
                    )
                try:
                    saved = runtime.store_upload(
                        item.filename or "upload.bin",
                        data,
                        thread_id=internal,
                        max_bytes=settings.web_max_upload_bytes,
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=str(exc)
                    ) from exc
                except Exception as exc:  # noqa: BLE001 - 沙箱/路径错误要可读报出
                    raise HTTPException(
                        status_code=503,
                        detail=f"{type(exc).__name__}: {exc}",
                    ) from exc
                config.uploaded_bytes += len(data)
                uploaded.append(
                    {
                        **saved,
                        "name": item.filename or "upload.bin",
                        "content_type": item.content_type or "",
                    }
                )
        finally:
            if config.uploaded_bytes != before_bytes:
                _set_session_config(user, tenant, config)
            await runtime.aclose()
        return {"uploads": uploaded}

    def _upload_rows(
        workspace: str, internal: str
    ) -> tuple[list[dict[str, Any]], int]:
        base = Path(workspace) / ".agent" / "uploads" / internal
        rows: list[dict[str, Any]] = []
        total = 0
        if not base.is_dir():
            return rows, total
        for path in sorted(base.rglob("*"))[:500]:
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total += size
            rows.append(
                {
                    "relative_path": path.relative_to(workspace).as_posix(),
                    "bytes": size,
                }
            )
        return rows, total

    @app.get("/api/sessions/{thread_id}/uploads")
    async def list_uploads(thread_id: str, user: UserDep) -> dict:
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        if agent_client is not None:
            data = await _agent_json(
                user, tenant, config, "GET",
                f"/v1/uploads/{public_thread}",
            )
            data["accounted_bytes"] = (
                config.uploaded_bytes if config else 0
            )
            data["workspace_bytes"] = _workspace_uploaded_bytes(
                user,
                config.workspace if config else tenant.workspace,
            )
            return data
        workspace = config.workspace if config else tenant.workspace
        internal = tenant.thread_id(public_thread)
        rows, actual = await asyncio.to_thread(
            _upload_rows, workspace, internal
        )
        return {
            "uploads": rows,
            "used_bytes": actual,
            "accounted_bytes": config.uploaded_bytes if config else 0,
            "max_file_bytes": settings.web_max_upload_bytes,
            "max_files_per_request": settings.web_max_upload_files,
            "max_total_bytes": settings.web_max_upload_total_bytes,
            "workspace_bytes": _workspace_uploaded_bytes(user, workspace),
        }

    @app.delete("/api/sessions/{thread_id}/uploads")
    async def delete_upload(
        thread_id: str,
        request: UploadDeleteRequest,
        user: UserDep,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        _reject_if_active(user, public_thread)
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if agent_client is not None:
            result = await _agent_json(
                user, tenant, config, "DELETE",
                f"/v1/uploads/{public_thread}",
                json_body={"path": request.path},
            )
            config.uploaded_bytes = max(
                0,
                config.uploaded_bytes - int(result.get("bytes", 0)),
            )
            _set_session_config(user, tenant, config)
            return result
        internal = tenant.thread_id(public_thread)
        prefix = f".agent/uploads/{internal}/"
        rel = request.path.strip().replace("\\", "/")
        if not rel.startswith(prefix):
            raise HTTPException(status_code=422, detail="只能删除本会话的上传")
        root = Path(config.workspace).resolve()
        try:
            target = (root / rel).resolve()
            target.relative_to(root)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="路径越界") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        size = target.stat().st_size
        target.unlink()
        config.uploaded_bytes = max(0, config.uploaded_bytes - size)
        _set_session_config(user, tenant, config)
        return {"ok": True, "bytes": size}

    @app.get("/api/recovery")
    async def recovery(user: UserDep) -> dict:
        """列出需要用户处理的会话：待审批、暂停、异常中断或仍在运行。"""
        await _sweep_expired()
        tenant = _tenant(user)
        _load_session_configs(user, tenant)
        if agent_client is not None:
            data = await _agent_json(
                user, tenant, None, "GET", "/v1/recovery"
            )
            rows = []
            for item in data.get("items", []):
                thread = str(item.get("thread_id") or "")
                if not thread:
                    continue
                if (user.name, thread) in deleted_sessions:
                    continue
                config = _config_for(
                    user, tenant, thread, create=False
                )
                rows.append(
                    {
                        **item,
                        "title": (
                            (config.title if config else "")
                            or item.get("title", "")
                        ),
                        "workspace": (
                            config.workspace
                            if config
                            else item.get("workspace", "")
                        ),
                    }
                )
            return {"items": rows}
        found = SessionIndex(
            tenant.settings.resolved_audit_dir
        ).list(limit=50)
        rows: list[dict[str, Any]] = []
        checked = 0
        for info in found:
            public_thread = tenant.public_thread(info.thread_id)
            if (user.name, public_thread) in deleted_sessions:
                continue
            config = _config_for(
                user, tenant, public_thread, create=False
            )
            entry = _get_entry(user.name, public_thread)
            internal = tenant.thread_id(public_thread)
            status = ""
            approvals = 0
            if entry is not None:
                if getattr(entry.runtime, "_stream_active", False):
                    status = "running"
                elif entry.runtime.is_paused(internal):
                    status = "paused"
                else:
                    try:
                        approvals = len(
                            await entry.runtime.pending_approvals(internal)
                        )
                    except Exception:  # noqa: BLE001
                        approvals = 0
                    if approvals:
                        status = "approval"
            elif info.interrupted and checked < 10:
                checked += 1
                runtime = _ops_runtime(tenant, config)
                try:
                    if runtime.is_paused(internal):
                        status = "paused"
                    else:
                        approvals = len(
                            await runtime.pending_approvals(internal)
                        )
                        if approvals:
                            status = "approval"
                        else:
                            status = "interrupted"
                except Exception:  # noqa: BLE001 - 探测失败仍按中断展示
                    status = "interrupted"
                finally:
                    await runtime.aclose()
            if not status:
                continue
            rows.append(
                {
                    "thread_id": public_thread,
                    "title": (
                        (config.title if config else "")
                        or info.title
                        or public_thread
                    ),
                    "workspace": (
                        config.workspace if config else info.workspace
                    ),
                    "last_active": info.last_seen,
                    "status": status,
                    "approvals": approvals,
                }
            )
        return {"items": rows}

    @app.get("/api/providers")
    async def providers(user: UserDep) -> dict:
        _apply_provider_profiles()
        return {
            "providers": _provider_rows(settings, _user_store(user)),
            "default_model": settings.deepseek_model,
            "server_write_enabled": allow_write,
            "profiles_enabled": not is_multi_user,
        }

    def _reject_profile_write() -> None:
        if is_multi_user:
            raise HTTPException(
                status_code=403,
                detail="多用户模式请用 AGENT_LLM_PROVIDERS 配置 provider",
            )

    @app.get("/api/provider-profiles")
    async def list_provider_profiles(user: UserDep) -> dict:
        profiles = _apply_provider_profiles()
        return {
            "profiles": [profile.to_item() for profile in profiles],
            "base_configured": bool(base_llm_providers),
            "profiles_enabled": not is_multi_user,
        }

    @app.post("/api/provider-profiles")
    async def upsert_provider_profile(
        payload: dict[str, Any], user: UserDep
    ) -> dict:
        _reject_profile_write()
        _rate_limit(user)
        try:
            profile = profile_from_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store = ProviderProfileStore(_profiles_path())
        profiles = store.upsert(profile)
        _apply_provider_profiles()
        return {
            "ok": True,
            "profiles": [item.to_item() for item in profiles],
        }

    @app.delete("/api/provider-profiles/{name}")
    async def delete_provider_profile(
        name: str, user: UserDep
    ) -> dict:
        _reject_profile_write()
        store = ProviderProfileStore(_profiles_path())
        profiles = store.delete(name)
        _apply_provider_profiles()
        return {
            "ok": True,
            "profiles": [item.to_item() for item in profiles],
        }

    @app.post("/api/providers/{name}/key")
    async def set_provider_key(
        name: str,
        request: ProviderKeyRequest,
        user: UserDep,
    ) -> dict:
        clean = re.sub(r"[^A-Za-z0-9_.-]", "", name)[:64]
        if not clean:
            raise HTTPException(status_code=422, detail="非法 provider 名")
        try:
            _user_store(user).set(clean, request.api_key)
        except SecretStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "name": clean, "has_key": True}

    @app.delete("/api/providers/{name}/key")
    async def delete_provider_key(name: str, user: UserDep) -> dict:
        clean = re.sub(r"[^A-Za-z0-9_.-]", "", name)[:64]
        _user_store(user).delete(clean)
        return {"ok": True, "name": clean, "has_key": False}

    @app.post("/api/providers/test")
    async def test_provider(
        request: ProviderTestRequest,
        user: UserDep,
    ) -> dict:
        """轻量连通性探测：只请求 `/models`，不发送用户内容、不记录 key。"""
        _apply_provider_profiles()
        spec = _resolve_provider(
            settings, _user_store(user), request.name
        )
        if spec is None:
            raise HTTPException(status_code=404, detail="provider 不存在")
        if not spec["api_key"]:
            raise HTTPException(status_code=422, detail="provider 还没有配置密钥")
        base_url = spec["base_url"].rstrip("/")
        try:
            parsed = httpx.URL(base_url)
        except (httpx.InvalidURL, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=422, detail="provider base_url 非法"
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise HTTPException(status_code=422, detail="provider base_url 非法")
        try:
            # 不走环境代理：避免把 Authorization 发给 HTTP_PROXY。
            async with httpx.AsyncClient(
                timeout=5.0, trust_env=False
            ) as client:
                response = await client.get(
                    f"{base_url}/models",
                    headers={
                        "Authorization": f"Bearer {spec['api_key']}",
                    },
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "reachable": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        ok = 200 <= response.status_code < 300
        models: list[str] = []
        if ok:
            try:
                parse = getattr(response, "json", None)
                payload = parse() if callable(parse) else {}
                rows = payload.get("data") if isinstance(payload, dict) else []
                models = [
                    str(row.get("id"))
                    for row in (rows or [])
                    if isinstance(row, dict) and row.get("id")
                ][:100]
            except Exception:  # noqa: BLE001 - /models 格式不标准不影响连通性
                models = []
        return {
            "ok": ok,
            "reachable": True,
            "status_code": response.status_code,
            "model": spec["model"],
            "models": models,
            "error": "" if ok else f"provider 返回 HTTP {response.status_code}",
        }

    @app.get("/api/approvals/{thread_id}")
    async def pending_approvals(
        thread_id: str,
        user: UserDep,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        tenant = _tenant(user)
        if agent_client is not None:
            config = _config_for(
                user, tenant, public_thread, create=False
            )
            try:
                return await agent_client.approvals(
                    _agent_context(
                        user, tenant, config, include_keys=True
                    ),
                    public_thread,
                )
            except Exception as exc:  # noqa: BLE001
                raise _agent_failure(exc) from exc
        entry = _get_entry(user.name, public_thread)
        temporary = None
        runtime = entry.runtime if entry is not None else None
        if runtime is None:
            config = _config_for(user, tenant, public_thread)
            temporary = _new_runtime(tenant, config)
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
    ) -> StreamingResponse:
        _rate_limit(user)
        await _sweep_expired()
        if len(request.prompt) > settings.web_max_prompt_chars:
            raise HTTPException(
                status_code=413,
                detail=f"prompt 超过上限 {settings.web_max_prompt_chars} 字符",
            )
        public_thread = _check_thread_id(request.thread_id)
        _reject_if_deleted(user, public_thread)
        if agent_client is not None:
            tenant = _tenant(user)
            config = _config_for(user, tenant, public_thread)
            if not config.title:
                config.title = " ".join(request.prompt.split())[:120]
                _set_session_config(user, tenant, config)
            try:
                stream = await agent_client.open_run(
                    AgentRunRequest(
                        context=_agent_context(
                            user, tenant, config, include_keys=True
                        ),
                        thread_id=public_thread,
                        prompt=request.prompt,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise _agent_failure(exc) from exc
            return await _proxy_agent(stream, public_thread=public_thread)
        key = (user.name, public_thread)
        with registry_lock:
            if runtimes.contains(key):
                raise HTTPException(
                    status_code=409,
                    detail="该会话已挂起或正在运行；请先 resume",
                )
        _stream_guard(key)
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread)
        runtime = _new_runtime(tenant, config)
        with registry_lock:
            runtimes.put(key, runtime)
        events = runtime.run(
            request.prompt,
            thread_id=tenant.thread_id(public_thread),
        )
        if stream_buffer:
            return _buffered_sse(
                _start_stream(key, public_thread, runtime, events)
            )
        return _sse_response(
            key,
            public_thread,
            runtime,
            events,
        )

    @app.post("/api/steer/{thread_id}")
    async def steer(
        thread_id: str,
        request: SteerRequest,
        user: UserDep,
    ) -> dict:
        _rate_limit(user)
        public_thread = _check_thread_id(thread_id)
        if agent_client is not None:
            tenant = _tenant(user)
            config = _config_for(
                user, tenant, public_thread, create=False
            )
            try:
                return await agent_client.steer(
                    _agent_context(user, tenant, config),
                    public_thread,
                    action=request.action,
                    text=request.text,
                    role=request.role,
                )
            except Exception as exc:  # noqa: BLE001
                raise _agent_failure(exc) from exc
        entry = _get_entry(user.name, public_thread)
        if entry is None:
            raise HTTPException(
                status_code=404, detail="该会话没有运行中的 agent"
            )
        tenant = _tenant(user)
        try:
            entry.runtime.steer(
                tenant.thread_id(public_thread),
                request.action,
                text=request.text,
                role=request.role,
                source=client_source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "action": request.action}

    @app.post("/api/resume/{thread_id}")
    async def resume(
        thread_id: str,
        request: ResumeRequest,
        user: UserDep,
    ) -> StreamingResponse:
        _rate_limit(user)
        await _sweep_expired()
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        if agent_client is not None:
            tenant = _tenant(user)
            config = _config_for(
                user, tenant, public_thread, create=False
            )
            try:
                stream = await agent_client.open_resume(
                    _agent_context(
                        user, tenant, config, include_keys=True
                    ),
                    public_thread,
                    request.decisions,
                )
            except Exception as exc:  # noqa: BLE001
                raise _agent_failure(exc) from exc
            return await _proxy_agent(stream, public_thread=public_thread)
        key = (user.name, public_thread)
        existing = _get_entry(user.name, public_thread)
        if existing is not None and getattr(
            existing.runtime, "_stream_active", False
        ):
            raise HTTPException(
                status_code=409,
                detail="该会话正在运行；不能同时 resume",
            )
        _stream_guard(key, require_suspended=True)
        key, runtime = await _runtime_for_resume(user, public_thread)
        # 等待 runtime 期间可能有并发请求；再检查一次，避免覆盖 active 流。
        _stream_guard(key, require_suspended=True)
        tenant = _tenant(user)
        internal = tenant.thread_id(public_thread)
        if runtime.is_paused(internal):
            raise HTTPException(
                status_code=409,
                detail="该会话是 steering 暂停；请用 /api/resume_steering",
            )
        events = runtime.resume(internal, request.decisions)
        if stream_buffer:
            return _buffered_sse(
                _start_stream(key, public_thread, runtime, events)
            )
        return _sse_response(key, public_thread, runtime, events)

    @app.post("/api/resume_steering/{thread_id}")
    async def resume_steering(
        thread_id: str,
        user: UserDep,
    ) -> StreamingResponse:
        _rate_limit(user)
        await _sweep_expired()
        public_thread = _check_thread_id(thread_id)
        _reject_if_deleted(user, public_thread)
        if agent_client is not None:
            tenant = _tenant(user)
            config = _config_for(
                user, tenant, public_thread, create=False
            )
            try:
                stream = await agent_client.open_resume_steering(
                    _agent_context(
                        user, tenant, config, include_keys=True
                    ),
                    public_thread,
                )
            except Exception as exc:  # noqa: BLE001
                raise _agent_failure(exc) from exc
            return await _proxy_agent(stream, public_thread=public_thread)
        key = (user.name, public_thread)
        _stream_guard(key)
        key, runtime = await _runtime_for_resume(user, public_thread)
        _stream_guard(key)
        tenant = _tenant(user)
        internal = tenant.thread_id(public_thread)
        # 只有 steering 暂停才能走这个入口。审批挂起的 resume payload 形状
        # 不同；误用它会让 approval_gate 把所有待批调用按 fail-closed 拒绝。
        if not runtime.is_paused(internal):
            raise HTTPException(
                status_code=409,
                detail="该会话未处于 steering 暂停状态；审批恢复请用 /api/resume",
            )
        events = runtime.resume_steering(internal)
        if stream_buffer:
            return _buffered_sse(
                _start_stream(key, public_thread, runtime, events)
            )
        return _sse_response(key, public_thread, runtime, events)

    @app.get("/api/status/{thread_id}")
    async def status(
        thread_id: str,
        user: UserDep,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        if agent_client is not None:
            tenant = _tenant(user)
            config = _config_for(
                user, tenant, public_thread, create=False
            )
            try:
                return await agent_client.status(
                    _agent_context(user, tenant, config), public_thread
                )
            except Exception as exc:  # noqa: BLE001
                raise _agent_failure(exc) from exc
        entry = _get_entry(user.name, public_thread)
        stream_entry = streams.get((user.name, public_thread))
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
        tenant = _tenant(user)
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
        if agent_client is None and (not stream_buffer or transport is None):
            raise HTTPException(status_code=404, detail="事件重放未启用")
        public_thread = _check_thread_id(thread_id)
        header = request.headers.get("last-event-id", "")
        try:
            header_seq = int(header)
        except (TypeError, ValueError):
            header_seq = 0
        start = max(0, int(after), header_seq)
        if agent_client is not None:
            tenant = _tenant(user)
            config = _config_for(
                user, tenant, public_thread, create=False
            )
            try:
                stream = await agent_client.open_stream(
                    _agent_context(user, tenant, config),
                    public_thread,
                    after=start,
                )
            except Exception as exc:  # noqa: BLE001
                raise _agent_failure(exc) from exc
            if stream.status_code == 204:
                await stream.aclose()
                return Response(status_code=204)
            return await _proxy_agent(
                stream, public_thread=public_thread
            )
        event_key = _event_key(user.name, public_thread)
        entry = streams.get((user.name, public_thread))
        if entry is not None:
            try:
                latest = transport.latest_seq(event_key)
            except Exception:  # noqa: BLE001 - 传输不可用时退回本地缓冲
                latest = entry.seq
            if latest > entry.seq:
                return _transport_sse(
                    event_key, public_thread, after=start
                )
            return _buffered_sse(entry, after=start)
        try:
            latest = transport.latest_seq(event_key)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503, detail="事件传输不可用"
            ) from exc
        if latest <= start:
            return Response(status_code=204)
        return _transport_sse(event_key, public_thread, after=start)

    @app.get("/api/thread/{thread_id}")
    async def thread_history(
        thread_id: str,
        user: UserDep,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        if agent_client is not None:
            try:
                return await agent_client.history(
                    _agent_context(user, tenant, config),
                    public_thread,
                    limit=limit,
                    offset=offset,
                )
            except Exception as exc:  # noqa: BLE001
                raise _agent_failure(exc) from exc
        runtime = _ops_runtime(tenant, config)
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
            ]
        }

    # ------------------------------------------------------------------
    # 快照 / 回滚 / checkpoint
    # ------------------------------------------------------------------

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

    @app.get("/api/snapshots")
    async def snapshots(
        user: UserDep,
        thread_id: str,
        limit: int = 20,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        tenant, config = _config_and_tenant(user, public_thread)
        if agent_client is not None:
            return await _agent_json(
                user, tenant, config, "GET", "/v1/snapshots",
                params={"thread_id": public_thread, "limit": limit},
            )
        runtime = _ops_runtime(tenant, config)
        try:
            entries = runtime.list_snapshots(limit=max(1, limit))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {"snapshots": [asdict(entry) for entry in entries]}

    @app.get("/api/diff")
    async def diff(
        user: UserDep,
        thread_id: str,
        path: str = "",
        snapshot_id: str = "",
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        tenant, config = _config_and_tenant(user, public_thread)
        if agent_client is not None:
            return await _agent_json(
                user, tenant, config, "GET", "/v1/diff",
                params={
                    "thread_id": public_thread,
                    "path": path,
                    "snapshot_id": snapshot_id,
                },
            )
        runtime = _ops_runtime(tenant, config)
        try:
            text = runtime.diff_snapshot(
                path=path or None,
                snapshot_id=snapshot_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {"diff": text}

    @app.post("/api/restore")
    async def restore(
        request: RestoreRequest,
        user: UserDep,
    ) -> dict:
        public_thread = _check_thread_id(request.thread_id)
        _reject_if_active(user, public_thread)
        tenant, config = _config_and_tenant(user, public_thread)
        if agent_client is not None:
            return await _agent_json(
                user, tenant, config, "POST", "/v1/restore",
                json_body=request.model_dump(),
            )
        runtime = _ops_runtime(tenant, config)
        try:
            if request.prefix:
                results = runtime.restore_since(
                    request.prefix, dry_run=request.dry_run
                )
                return {"results": [asdict(item) for item in results]}
            result = runtime.restore(
                path=request.path or None,
                snapshot_id=request.snapshot_id or None,
            )
            return asdict(result)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()

    @app.get("/api/run_started/{thread_id}")
    async def run_started(
        thread_id: str,
        user: UserDep,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        tenant, config = _config_and_tenant(user, public_thread)
        if agent_client is not None:
            return await _agent_json(
                user, tenant, config, "GET",
                f"/v1/run_started/{public_thread}",
            )
        runtime = _ops_runtime(tenant, config)
        try:
            prefix = runtime.last_run_started_at(
                tenant.thread_id(public_thread)
            )
        finally:
            await runtime.aclose()
        return {"prefix": prefix}

    @app.get("/api/checkpoints/{thread_id}")
    async def checkpoints(
        thread_id: str,
        user: UserDep,
        limit: int = 20,
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        tenant, config = _config_and_tenant(user, public_thread)
        if agent_client is not None:
            return await _agent_json(
                user, tenant, config, "GET",
                f"/v1/checkpoints/{public_thread}",
                params={"limit": limit},
            )
        runtime = _ops_runtime(tenant, config)
        try:
            found = await runtime.checkpoints(
                tenant.thread_id(public_thread),
                limit=max(1, limit),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {"checkpoints": found}

    @app.post("/api/fork/{thread_id}")
    async def fork(
        thread_id: str,
        user: UserDep,
        checkpoint_id: str = "",
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        _reject_if_active(user, public_thread)
        tenant, config = _config_and_tenant(user, public_thread)
        if agent_client is not None:
            result = await _agent_json(
                user, tenant, config, "POST",
                f"/v1/fork/{public_thread}",
                params={"checkpoint_id": checkpoint_id},
            )
            target_public = str(result.get("thread_id") or "")
            if target_public:
                _set_session_config(
                    user,
                    tenant,
                    SessionConfig(
                        thread_id=target_public,
                        workspace=(
                            config.workspace
                            if config
                            else tenant.workspace
                        ),
                        access_mode=_effective_access(config),
                        model=config.model if config else "",
                        provider=config.provider if config else "",
                    ),
                )
            return result
        runtime = _ops_runtime(tenant, config)
        try:
            target_internal = await runtime.fork(
                tenant.thread_id(public_thread),
                checkpoint_id=checkpoint_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=409,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        target_public = tenant.public_thread(target_internal)
        _set_session_config(
            user,
            tenant,
            SessionConfig(
                thread_id=target_public,
                workspace=config.workspace if config else tenant.workspace,
                access_mode=_effective_access(config),
                model=config.model if config else "",
                provider=config.provider if config else "",
            ),
        )
        return {"thread_id": target_public}

    @app.post("/api/rewind/{thread_id}")
    async def rewind(
        thread_id: str,
        user: UserDep,
        steps: int = 1,
        checkpoint_id: str = "",
    ) -> dict:
        public_thread = _check_thread_id(thread_id)
        _reject_if_active(user, public_thread)
        tenant, config = _config_and_tenant(user, public_thread)
        if agent_client is not None:
            return await _agent_json(
                user, tenant, config, "POST",
                f"/v1/rewind/{public_thread}",
                params={
                    "steps": max(1, steps),
                    "checkpoint_id": checkpoint_id,
                },
            )
        runtime = _ops_runtime(tenant, config)
        try:
            found = await runtime.rewind(
                tenant.thread_id(public_thread),
                checkpoint_id=checkpoint_id,
                steps=max(1, steps),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=409,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {"checkpoint_id": found}

    # ------------------------------------------------------------------
    # 记忆 / 审计
    # ------------------------------------------------------------------

    @app.get("/api/memory")
    async def memory(
        user: UserDep,
        scope: str = "project",
    ) -> dict:
        tenant = _tenant(user)
        if agent_client is not None:
            return await _agent_json(
                user, tenant, None, "GET", "/v1/memory",
                params={"scope": scope},
            )
        runtime = _ops_runtime(tenant, None)
        try:
            facts = runtime.memories(scope=scope)
        finally:
            await runtime.aclose()
        return {"facts": facts}

    @app.post("/api/memory")
    async def memory_write(
        request: MemoryRequest,
        user: UserDep,
    ) -> dict:
        _rate_limit(user)
        tenant = _tenant(user)
        if agent_client is not None:
            return await _agent_json(
                user, tenant, None, "POST", "/v1/memory",
                json_body=request.model_dump(),
            )
        runtime = _ops_runtime(tenant, None)
        internal = tenant.thread_id(request.thread_id) if request.thread_id else ""
        try:
            if request.action == "remember":
                facts = runtime.remember(
                    request.text,
                    thread_id=internal,
                    scope=request.scope,
                )
            elif request.action == "forget":
                facts = runtime.forget(
                    request.index,
                    thread_id=internal,
                    scope=request.scope,
                )
            elif request.action == "clear":
                facts = runtime.clear_memory(
                    thread_id=internal, scope=request.scope
                )
            elif request.action == "pin":
                facts = runtime.pin_memory(
                    request.index,
                    pinned=request.pinned,
                    thread_id=internal,
                    scope=request.scope,
                )
            else:
                facts = runtime.set_memory_priority(
                    request.index,
                    request.priority,
                    thread_id=internal,
                    scope=request.scope,
                )
        except (IndexError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            await runtime.aclose()
        return {"facts": facts}

    @app.get("/api/audit")
    async def audit(
        user: UserDep,
        thread_id: str = "",
        limit: int = 100,
    ) -> dict:
        tenant = _tenant(user)
        if agent_client is not None:
            data = await _agent_json(
                user, tenant, None, "GET", "/v1/audit",
                params={
                    "thread_id": (
                        _check_thread_id(thread_id) if thread_id else ""
                    ),
                    "limit": max(1, limit),
                },
            )
            if control_store is not None:
                try:
                    control_store.index_audit(
                        user.name, list(data.get("records", []))
                    )
                except Exception:  # noqa: BLE001 - 索引失败不影响查询
                    _LOG.warning("审计索引写入失败", exc_info=True)
            return data
        runtime = _ops_runtime(tenant, None)
        try:
            internal = (
                tenant.thread_id(_check_thread_id(thread_id))
                if thread_id
                else None
            )
            records = read_records_many(
                runtime.audit_files,
                thread_id=internal,
                limit=max(1, limit),
            )
        finally:
            await runtime.aclose()
        return {
            "records": [
                record.model_dump(mode="json") for record in records
            ]
        }

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

    return app
