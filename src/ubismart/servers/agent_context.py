"""A3 的租户上下文与 runtime 构造（P1.2）。

把 `create_agent_app` 里的 tenant 目录推导、workspace 校验与 runtime 构造
独立出来，HTTP 路由只负责请求/响应。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ubismart.agent_protocol import AgentContext
from ubismart.config import Settings
from ubismart.execution.host_backend import HostExecutionBackend
from ubismart.execution.host_file_access import HostFileAccess
from ubismart.execution.host_fs import HostFs
from ubismart.execution.host_snapshots import HostSnapshotStore
from ubismart.identity.access import normalize_access_mode
from ubismart.runtime import AgentRuntime
from ubismart.sandbox.pathguard import (
    PathGuardError,
    ensure_inside,
    is_within,
    normalize_root,
)
from ubismart.servers.agent_server_support import _ContextSecretStore, _safe_tenant
from ubismart.workspace.model import normalize_workspace_kind


class AgentRuntimeFactory:
    def __init__(
        self,
        settings: Settings,
        runtime_factory: Any = AgentRuntime,
        *,
        host_broker_acceptor: Any | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_factory = runtime_factory
        self.host_broker_acceptor = host_broker_acceptor

    def tenant_settings(
        self, context: AgentContext, *, validate_library_root: bool = False
    ) -> Settings:
        tenant = _safe_tenant(context.tenant)
        base = Path(
            self.settings.agent_data_root
            or (self.settings.resolved_audit_dir.parent / "agent")
        ) / tenant
        base.mkdir(parents=True, exist_ok=True)
        library_root = context.library_root
        if validate_library_root:
            library_root = self._validated_library_root(context)
        return self.settings.model_copy(
            update={
                "audit_dir": str(base / "audit"),
                "checkpoint_path": str(base / "checkpoints.sqlite"),
                "steering_dir": str(base / "steering"),
                "memory_root": str(base / "memory"),
                "memory_global_root": str(base / "memory" / "global"),
                # 资料库由 A2 按用户指定；A3 不直接信任，必须校验或自行推导。
                "library_root": library_root,
                "wsl_workspace": context.workspace,
            }
        )

    def _validated_library_root(self, context: AgentContext) -> str:
        """校验 A2 传来的资料库根，必要时按租户推导。

        A2 是认证与租户来源，但 A3 不能把任意绝对路径当成事实：一旦
        内部 token/HMAC 泄漏，未校验的 root 会变成跨租户读删的入口。
        """
        tenant = _safe_tenant(context.tenant)
        requested = str(context.library_root or "").strip()
        base = str(
            self.settings.web_library_root
            or self.settings.library_root
            or ""
        ).strip()
        if requested:
            if "\x00" in requested or not requested.startswith("/"):
                raise HTTPException(
                    status_code=400,
                    detail="library_root 必须是 WSL 绝对路径",
                )
            requested = normalize_root(requested)

        if base:
            base = normalize_root(base)
            if not requested:
                # 单用户沿用配置根；多用户必须落到该用户自己的子目录。
                if tenant == "local":
                    return base
                return ensure_inside(f"{base}/{tenant}", base)
            try:
                safe = ensure_inside(requested, base)
            except PathGuardError as exc:
                raise HTTPException(
                    status_code=403,
                    detail="library_root 不在允许的资料库根内",
                ) from exc
            if safe == base:
                if tenant != "local":
                    raise HTTPException(
                        status_code=403,
                        detail="多租户部署的资料库根必须按用户隔离",
                    )
                return safe
            expected = ensure_inside(f"{base}/{tenant}", base)
            if safe != expected and not is_within(safe, expected):
                raise HTTPException(
                    status_code=403,
                    detail="library_root 必须位于当前租户目录内",
                )
            return safe

        if not requested:
            # 未配置根时，只有单用户模式可以沿用沙箱家目录的默认位置。
            if tenant == "local":
                return ""
            raise HTTPException(
                status_code=400,
                detail=(
                    "多租户部署必须提供 library_root，"
                    "或配置 AGENT_WEB_LIBRARY_ROOT / AGENT_LIBRARY_ROOT"
                ),
            )
        if requested.rstrip("/").rsplit("/", 1)[-1] != tenant:
            raise HTTPException(
                status_code=403,
                detail="library_root 必须位于当前租户目录内",
            )
        return requested

    def new(self, context: AgentContext) -> AgentRuntime:
        kind = normalize_workspace_kind(context.workspace_kind)
        profile = str(self.settings.security_profile or "").lower()
        if kind == "host" and profile == "untrusted":
            raise HTTPException(
                status_code=400,
                detail=(
                    "不可信仓库禁止使用 host 工作区；"
                    "请改用 sandbox（WSL2）"
                ),
            )
        access = normalize_access_mode(context.access_mode) or "read_only"
        file_access = None
        targets = None
        host_fs = None
        host_snapshots = None
        host_cache: dict[str, bytes] = {}
        workspace = context.workspace
        if kind == "host":
            if self.host_broker_acceptor is None:
                raise HTTPException(
                    status_code=503, detail="A2L acceptor 未启用"
                )
            caller = self.host_broker_acceptor.caller_for(context.a2l_id)
            if caller is None:
                raise HTTPException(
                    status_code=503, detail="A2L 未连接"
                )
            tenant = _safe_tenant(context.tenant)
            base = Path(
                self.settings.agent_data_root
                or (self.settings.resolved_audit_dir.parent / "agent")
            ) / tenant
            host_root = base / "host-workspace"
            host_root.mkdir(parents=True, exist_ok=True)
            workspace = str(host_root)
            file_access = HostFileAccess(
                caller,
                workspace_id=context.workspace_id,
                root_handle=context.root_handle,
                cache=host_cache,
            )
            host_fs = HostFs(
                caller,
                workspace_id=context.workspace_id,
                root_handle=context.root_handle,
                cache=host_cache,
            )
            host_snapshots = HostSnapshotStore(
                caller,
                workspace_id=context.workspace_id,
                root_handle=context.root_handle,
            )
            targets = {
                "host": HostExecutionBackend(
                    caller,
                    workspace_id=context.workspace_id,
                    root_handle=context.root_handle,
                )
            }
        elif not self.settings.workspace_registry:
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
            self.tenant_settings(context, validate_library_root=True),
            workspace=workspace,
            allow_write=bool(context.allow_write),
            approval_mode="ask",
            access_mode=access,
            model_override=context.model,
            provider_name=context.provider,
            secret_store=_ContextSecretStore(context.api_keys),
            file_access=file_access,
            execution_targets=targets,
            workspace_kind=kind,
            host_fs=host_fs,
            host_snapshots=host_snapshots,
        )
