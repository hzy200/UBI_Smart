"""A2 Web 的会话目录、Provider 与 Agent 代理服务（P1.2 收口）。

这些服务从 `create_app` 的闭包中下沉，路由与装配层只负责注入依赖。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from coding_agent.access import normalize_access_mode
from coding_agent.agent_protocol import AgentContext
from coding_agent.config import Settings
from coding_agent.control.agent_client import AgentClientError
from coding_agent.memory.sessions import SessionIndex
from coding_agent.provider_profiles import (
    ProviderProfileStore,
    merge_provider_items,
)
from coding_agent.secrets_store import SecretStore
from coding_agent.tenancy import WebTenant
from coding_agent.web.auth import AuthUser
from coding_agent.web.models import SessionConfig

_LOG = logging.getLogger(__name__)


class ProviderService:
    def __init__(
        self,
        settings: Settings,
        *,
        base_llm_providers: str,
        is_multi_user: bool,
    ) -> None:
        self.settings = settings
        self.base_llm_providers = base_llm_providers
        self.is_multi_user = is_multi_user

    def profiles_path(self) -> Path:
        return Path(self.settings.resolved_audit_dir).parent / (
            "provider-profiles.json"
        )

    def apply(self) -> list:
        """把 GUI 档案合并进 llm_providers；没有档案时恢复环境基线。"""
        if self.is_multi_user:
            return []
        store = ProviderProfileStore(self.profiles_path())
        profiles = store.load()
        if profiles:
            items = merge_provider_items(self.base_llm_providers, profiles)
            self.settings.llm_providers = json.dumps(
                items, ensure_ascii=False
            )
        elif self.settings.llm_providers != self.base_llm_providers:
            self.settings.llm_providers = self.base_llm_providers
        return profiles

    def user_store(self, user: AuthUser) -> SecretStore:
        return (
            SecretStore(f"coding-agent:{user.name}")
            if self.is_multi_user
            else SecretStore()
        )

    def tenant_store(self, tenant: WebTenant) -> SecretStore:
        return (
            SecretStore(f"coding-agent:{tenant.user}")
            if self.is_multi_user
            else SecretStore()
        )

    def rows(
        self, cfg: Settings, store: SecretStore
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

    def validate(
        self, cfg: Settings, store: SecretStore, provider: str
    ) -> None:
        if not provider:
            return
        names = {row["name"] for row in self.rows(cfg, store)}
        if provider not in names:
            raise HTTPException(
                status_code=422,
                detail=f"未知或未启用的 provider：{provider}",
            )

    def resolve(
        self, cfg: Settings, store: SecretStore, name: str
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


_TERMINAL_MARKERS = (
    b'"type": "run_finished"',
    b'"type": "run_failed"',
    b'"type": "approval_requested"',
    b'"type":"run_finished"',
    b'"type":"run_failed"',
    b'"type":"approval_requested"',
)


def _terminal_marker(buffer: bytes) -> bool:
    if any(marker in buffer for marker in _TERMINAL_MARKERS):
        return True
    if b"steering_applied" in buffer and b'"pause"' in buffer:
        return True
    return False


class AgentGateway:
    def __init__(
        self,
        agent_client: Any,
        providers: ProviderService,
        *,
        effective_access: Callable[[Any], str | None],
        allow_write: bool,
    ) -> None:
        self.agent_client = agent_client
        self.providers = providers
        self.effective_access = effective_access
        self.allow_write = allow_write

    def keys(self, user: AuthUser, tenant: WebTenant) -> dict[str, str]:
        """把 A2 持有的 provider key 短期带给 A3；不写日志/数据库。"""
        try:
            store = self.providers.tenant_store(tenant)
            keys: dict[str, str] = {}
            for row in self.providers.rows(tenant.settings, store):
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

    def context(
        self,
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
            workspace_id=config.workspace_id if config else "",
            access_mode=self.effective_access(config),
            model=config.model if config else "",
            provider=config.provider if config else "",
            allow_write=self.allow_write,
            api_keys=(
                self.keys(user, tenant) if include_keys else {}
            ),
        )

    def failure(self, exc: Exception) -> HTTPException:
        if isinstance(exc, AgentClientError):
            return HTTPException(status_code=exc.status_code, detail=exc.detail)
        return HTTPException(
            status_code=502,
            detail=f"Agent Server 不可用：{type(exc).__name__}: {exc}",
        )

    async def proxy(
        self,
        stream: Any,
        *,
        public_thread: str = "",
        on_progress: Any | None = None,
        on_terminal: Any | None = None,
    ) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            buffer = b""
            fired = False
            async for chunk in stream.aiter_bytes():
                if on_progress is not None:
                    on_progress()
                yield chunk
                if on_terminal is not None and not fired:
                    buffer = (buffer + chunk)[-4096:]
                    marker = _terminal_marker(buffer)
                    if marker:
                        fired = True
                        on_terminal()

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

    async def json(
        self,
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
            return await self.agent_client.json_request(
                self.context(
                    user, tenant, config, include_keys=include_keys
                ),
                method,
                path,
                params=params,
                json=json_body,
            )
        except Exception as exc:  # noqa: BLE001
            raise self.failure(exc) from exc

    async def upload(
        self,
        user: AuthUser,
        tenant: WebTenant,
        config: SessionConfig | None,
        thread_id: str,
        files: list[tuple[str, bytes, str]],
    ) -> dict:
        try:
            return await self.agent_client.upload_files(
                self.context(user, tenant, config),
                thread_id,
                files,
            )
        except Exception as exc:  # noqa: BLE001
            raise self.failure(exc) from exc


class SessionCatalog:
    def __init__(
        self,
        *,
        tenants: Any,
        tenant: Callable[[AuthUser], WebTenant],
        default_access: str | None,
        control_store: Any,
        control_db_url: str,
        session_configs: dict[tuple[str, str], SessionConfig],
        deleted_sessions: set[tuple[str, str]],
        sessions_loaded: set[str],
        registry_lock: Any,
        session_file_lock: Any,
        db_status: Callable[[], dict],
        mark_db_degraded: Callable[[Exception], None],
    ) -> None:
        self.tenants = tenants
        self.tenant = tenant
        self.default_access = default_access
        self.control_store = control_store
        self.control_db_url = control_db_url
        self.session_configs = session_configs
        self.deleted_sessions = deleted_sessions
        self.sessions_loaded = sessions_loaded
        self.registry_lock = registry_lock
        self.session_file_lock = session_file_lock
        self.db_status = db_status
        self.mark_db_degraded = mark_db_degraded

    def _db_degraded(self) -> bool:
        return bool(self.control_db_url) and (
            self.control_store is None
            or self.db_status().get("state") == "degraded"
        )

    def session_file(self, tenant: WebTenant) -> Path:
        return Path(tenant.settings.resolved_audit_dir) / "gui-sessions.json"

    def read_legacy(self, path: Path) -> list[dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [
            row for row in payload if isinstance(row, dict)
        ] if isinstance(payload, list) else []

    def install_rows(
        self,
        user: AuthUser,
        tenant: WebTenant,
        rows: list[dict[str, Any]],
        deleted: set[str] | None = None,
    ) -> None:
        with self.registry_lock:
            for thread in deleted or ():
                self.deleted_sessions.add((user.name, thread))
            for row in rows:
                thread = str(row.get("thread_id") or "")
                if not thread:
                    continue
                if row.get("deleted"):
                    self.deleted_sessions.add((user.name, thread))
                    continue
                self.session_configs.setdefault(
                    (user.name, thread),
                    SessionConfig(
                        thread_id=thread,
                        workspace=str(
                            row.get("workspace") or tenant.workspace
                        ),
                        workspace_id=str(row.get("workspace_id") or ""),
                        access_mode=normalize_access_mode(
                            row.get("access_mode")
                        ),
                        model=str(row.get("model") or ""),
                        provider=str(row.get("provider") or ""),
                        uploaded_bytes=int(row.get("uploaded_bytes") or 0),
                        title=str(row.get("title") or ""),
                    ),
                )

    def load(self, user: AuthUser, tenant: WebTenant) -> None:
        with self.registry_lock:
            if user.name in self.sessions_loaded:
                return
            self.sessions_loaded.add(user.name)
        path = self.session_file(tenant)
        legacy = self.read_legacy(path)
        if self.control_store is not None and not self._db_degraded():
            try:
                active, tombstones = self.control_store.load_sessions(
                    user.name
                )
                if not active and not tombstones and legacy:
                    self.control_store.save_sessions(
                        user.name,
                        [row for row in legacy if not row.get("deleted")],
                        {
                            str(row.get("thread_id"))
                            for row in legacy
                            if row.get("deleted") and row.get("thread_id")
                        },
                    )
                    active, tombstones = self.control_store.load_sessions(
                        user.name
                    )
                self.install_rows(user, tenant, active, tombstones)
                return
            except Exception as exc:  # noqa: BLE001 - 降级为本地文件
                self.mark_db_degraded(exc)
                _LOG.warning(
                    "控制面数据库读取失败，改用本地会话文件",
                    exc_info=True,
                )
        self.install_rows(user, tenant, legacy)

    def persist(self, user: AuthUser, tenant: WebTenant) -> None:
        with self.registry_lock:
            rows = [
                {
                    "thread_id": config.thread_id,
                    "workspace": config.workspace,
                    "workspace_id": config.workspace_id,
                    "access_mode": config.access_mode,
                    "model": config.model,
                    "provider": config.provider,
                    "uploaded_bytes": config.uploaded_bytes,
                    "title": config.title,
                }
                for (owner, _), config in self.session_configs.items()
                if owner == user.name
            ]
            tombstones = {
                thread
                for owner, thread in self.deleted_sessions
                if owner == user.name
            }
        if self.control_store is not None:
            if self.db_status().get("state") == "degraded":
                return
            try:
                self.control_store.save_sessions(
                    user.name, rows, tombstones
                )
            except Exception as exc:  # noqa: BLE001 - 运行中故障也要可预期
                self.mark_db_degraded(exc)
                _LOG.warning(
                    "控制面数据库写入失败，进入只读降级", exc_info=True
                )
            return
        if self.control_db_url:
            # 启动即降级：不写 JSON，避免与恢复后的数据库分叉。
            return
        rows.extend(
            {"thread_id": thread, "deleted": True}
            for thread in tombstones
        )
        path = self.session_file(tenant)
        with self.session_file_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(temp, path)
            except OSError:
                pass

    def audit_workspace(
        self, user: AuthUser, tenant: WebTenant, public_thread: str
    ) -> str:
        try:
            info = SessionIndex(tenant.settings.resolved_audit_dir).get(
                tenant.thread_id(public_thread)
            )
        except Exception:  # noqa: BLE001
            return ""
        if info is None or not info.workspace:
            return ""
        try:
            return self.tenants.resolve_workspace(user.name, info.workspace)
        except Exception:  # noqa: BLE001
            return ""

    def config_for(
        self,
        user: AuthUser,
        tenant: WebTenant,
        public_thread: str,
        *,
        create: bool = True,
    ) -> SessionConfig | None:
        self.load(user, tenant)
        key = (user.name, public_thread)
        with self.registry_lock:
            config = self.session_configs.get(key)
        if config is not None or not create:
            return config
        workspace = (
            self.audit_workspace(user, tenant, public_thread)
            or tenant.workspace
        )
        config = SessionConfig(
            thread_id=public_thread,
            workspace=workspace,
            access_mode=self.default_access,
        )
        with self.registry_lock:
            self.session_configs.setdefault(key, config)
            result = self.session_configs[key]
        self.persist(user, tenant)
        return result

    def set_config(
        self,
        user: AuthUser,
        tenant: WebTenant,
        config: SessionConfig,
    ) -> None:
        with self.registry_lock:
            self.session_configs[(user.name, config.thread_id)] = config
        self.persist(user, tenant)

    def is_deleted(self, user: AuthUser, public_thread: str) -> bool:
        self.load(user, self.tenant(user))
        with self.registry_lock:
            return (user.name, public_thread) in self.deleted_sessions

    def reject_deleted(self, user: AuthUser, public_thread: str) -> None:
        if self.is_deleted(user, public_thread):
            raise HTTPException(status_code=410, detail="会话已删除")

    def workspace_uploaded_bytes(
        self, user: AuthUser, workspace: str
    ) -> int:
        with self.registry_lock:
            return sum(
                config.uploaded_bytes
                for (owner, _), config in self.session_configs.items()
                if owner == user.name and config.workspace == workspace
            )
