"""Web 的同进程 A3 回退（P1.2）。

A2 在未配置 `AGENT_INTERNAL_URL` 时仍可在本进程创建 AgentRuntime。把这个
适配器独立出来，`web/app.py` 不再直接 import `runtime`；拆分模式下 A2 只
需要 HTTP 客户端。

这个模块是 A2 → A3 的唯一允许例外，不要扩散到其他 web 模块。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from coding_agent.access import ACCESS_READ_ONLY
from coding_agent.runtime import AgentRuntime
from coding_agent.tenancy import WebTenant


@dataclass
class RuntimeEntry:
    runtime: AgentRuntime
    last_active: float = field(default_factory=time.monotonic)


class LocalRuntimeRegistry:
    """运行配额、TTL 与 runtime 生命周期；同进程模式专用。"""

    def __init__(
        self,
        *,
        allow_write: bool,
        governor: Any,
        default_access: str | None,
        approval_mode: str,
        max_runtimes: int,
        max_per_user: int,
        runtime_ttl: float,
        secret_store_for: Callable[[WebTenant], Any],
    ) -> None:
        self.allow_write = allow_write
        self.governor = governor
        self.default_access = default_access
        self.approval_mode = approval_mode
        self.max_runtimes = max(1, int(max_runtimes or 1))
        self.max_per_user = max(1, int(max_per_user or 1))
        self.runtime_ttl = max(0.0, float(runtime_ttl or 0))
        self.secret_store_for = secret_store_for
        self.entries: dict[tuple[str, str], RuntimeEntry] = {}
        self.lock = threading.Lock()

    def effective_access(self, config: Any | None) -> str | None:
        """服务端 allow_write 是硬上限；持久化配置不能反向提权。"""
        access = (
            getattr(config, "access_mode", None)
            if config is not None
            else self.default_access
        )
        if access and access != ACCESS_READ_ONLY and not self.allow_write:
            return ACCESS_READ_ONLY
        return access

    def count_user(self, user: str) -> int:
        with self.lock:
            return sum(1 for key in self.entries if key[0] == user)

    def new(self, tenant: WebTenant, config: Any | None = None) -> AgentRuntime:
        with self.lock:
            total = len(self.entries)
            per_user = sum(1 for key in self.entries if key[0] == tenant.user)
        if total >= self.max_runtimes or per_user >= self.max_per_user:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"运行中的会话数已达上限"
                    f"（用户 {per_user}/{self.max_per_user}，"
                    f"全局 {total}/{self.max_runtimes}）"
                ),
            )
        # Web 默认逐条确认；但 production/deny 档不能被这里的 ask 覆盖。
        mode = (
            "deny"
            if str(tenant.settings.approval_mode).lower() == "deny"
            else self.approval_mode
        )
        return AgentRuntime(
            tenant.settings,
            workspace=(config.workspace or None) if config else None,
            allow_write=self.allow_write,
            governor=self.governor,
            approval_mode=mode,
            access_mode=self.effective_access(config) or None,
            model_override=(config.model if config else ""),
            provider_name=(config.provider if config else ""),
            secret_store=self.secret_store_for(tenant),
        )

    def ops(self, tenant: WebTenant, config: Any | None) -> AgentRuntime:
        """给快照/记忆/审计等宿主操作临时建 runtime，不占运行配额。"""
        return AgentRuntime(
            tenant.settings,
            workspace=(config.workspace or None) if config else None,
            allow_write=self.allow_write,
            governor=self.governor,
            approval_mode=self.approval_mode,
            access_mode=(self.effective_access(config) or None)
            if config
            else None,
            model_override=(config.model if config else ""),
            provider_name=(config.provider if config else ""),
            secret_store=self.secret_store_for(tenant),
        )

    def put(self, key: tuple[str, str], runtime: AgentRuntime) -> None:
        with self.lock:
            self.entries[key] = RuntimeEntry(runtime)

    def get(self, key: tuple[str, str]) -> RuntimeEntry | None:
        with self.lock:
            return self.entries.get(key)

    def contains(self, key: tuple[str, str]) -> bool:
        with self.lock:
            return key in self.entries

    def items(self) -> list[tuple[tuple[str, str], RuntimeEntry]]:
        with self.lock:
            return list(self.entries.items())

    def values(self) -> list[RuntimeEntry]:
        with self.lock:
            return list(self.entries.values())

    async def drop(self, key: tuple[str, str], runtime: AgentRuntime) -> None:
        with self.lock:
            entry = self.entries.get(key)
            if entry is not None and entry.runtime is runtime:
                self.entries.pop(key, None)
        try:
            await runtime.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不覆盖原始事件
            pass

    async def sweep(self, *, now: float | None = None) -> None:
        if self.runtime_ttl <= 0:
            return
        current = time.monotonic() if now is None else now
        expired: list[AgentRuntime] = []
        with self.lock:
            for key, entry in list(self.entries.items()):
                # 正在流式执行的 runtime 不能关：它还在 yield 事件。
                if getattr(entry.runtime, "_stream_active", False):
                    continue
                if current - entry.last_active > self.runtime_ttl:
                    self.entries.pop(key, None)
                    expired.append(entry.runtime)
        for runtime in expired:
            try:
                await runtime.aclose()
            except Exception:  # noqa: BLE001 - 清理失败不影响新请求
                pass
