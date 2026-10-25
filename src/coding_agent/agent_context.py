"""A3 的租户上下文与 runtime 构造（P1.2）。

把 `create_agent_app` 里的 tenant 目录推导、workspace 校验与 runtime 构造
独立出来，HTTP 路由只负责请求/响应。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from coding_agent.access import normalize_access_mode
from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_support import _ContextSecretStore, _safe_tenant
from coding_agent.config import Settings
from coding_agent.runtime import AgentRuntime


class AgentRuntimeFactory:
    def __init__(
        self,
        settings: Settings,
        runtime_factory: Any = AgentRuntime,
    ) -> None:
        self.settings = settings
        self.runtime_factory = runtime_factory

    def tenant_settings(self, context: AgentContext) -> Settings:
        tenant = _safe_tenant(context.tenant)
        base = Path(
            self.settings.agent_data_root
            or (self.settings.resolved_audit_dir.parent / "agent")
        ) / tenant
        base.mkdir(parents=True, exist_ok=True)
        return self.settings.model_copy(
            update={
                "audit_dir": str(base / "audit"),
                "checkpoint_path": str(base / "checkpoints.sqlite"),
                "steering_dir": str(base / "steering"),
                "memory_root": str(base / "memory"),
                "memory_global_root": str(base / "memory" / "global"),
                "wsl_workspace": context.workspace,
            }
        )

    def new(self, context: AgentContext) -> AgentRuntime:
        access = normalize_access_mode(context.access_mode) or "read_only"
        if not self.settings.workspace_registry:
            # 旧语义：A3 负责创建目录，保证单机/同进程部署可用。
            try:
                Path(context.workspace).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"无法创建 workspace：{type(exc).__name__}: {exc}"
                    ),
                ) from exc
        # 注册表开启时不再创建目录：工作区由 A2 注册，A3 只校验路径。
        return self.runtime_factory(
            self.tenant_settings(context),
            workspace=context.workspace,
            allow_write=bool(context.allow_write),
            approval_mode="ask",
            access_mode=access,
            model_override=context.model,
            provider_name=context.provider,
            secret_store=_ContextSecretStore(context.api_keys),
        )
