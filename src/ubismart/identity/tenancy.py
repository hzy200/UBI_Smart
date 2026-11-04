"""Web 租户隔离：每个用户独立的 workspace、宿主数据目录与 WSL 记忆目录。

关键边界：
- workspace / memory 在 WSL 内，由 `SandboxFs.mkdir` 创建（不拼 shell）；
- audit / checkpoint / steering 在**宿主**文件系统（AuditLogger、SQLite、
  SteeringController 都跑在宿主）；
- 用户只能得到 `workspaces_root/<user>`，不能指定任意主机路径。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ubismart.config import Settings
from ubismart.sandbox.backend import build_sandbox
from ubismart.sandbox.fs import SandboxFs, SandboxFsError
from ubismart.sandbox.pathguard import (
    PathGuardError,
    SandboxPathError,
    ensure_inside,
    normalize_root,
)

_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class TenantError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WebTenant:
    user: str
    multi_user: bool
    workspace: str
    settings: Settings
    data_dir: Path | None = None
    memory_dir: str = ""
    # 每用户跨工作区资料库根目录（WSL 内）；与 workspace 无关。
    library_dir: str = ""
    # 企业账号所属组织；仅用于集中密钥/报表作用域，不参与工作区路径。
    org: str = ""

    def thread_id(self, public_thread: str) -> str:
        """内部 thread id；多用户加 user 前缀，避免共享限流文件里串号。"""
        return f"{self.user}:{public_thread}" if self.multi_user else public_thread

    def public_thread(self, internal: str) -> str:
        prefix = f"{self.user}:"
        if self.multi_user and internal.startswith(prefix):
            return internal[len(prefix) :]
        return internal


class TenantManager:
    def __init__(
        self,
        settings: Settings,
        *,
        sandbox: Any | None = None,
        governor: object | None = None,
        multi_user: bool | None = None,
    ) -> None:
        self.settings = settings
        # Web 层不应直接构造沙箱（架构约束）；由这里集中处理，
        # 并优先复用传入的 governor 的 WSL 槽位。
        slots = getattr(governor, "wsl", None) if governor is not None else None
        # 租户目录创建是**宿主管理动作**（mkdir/$HOME），不执行模型意图。
        # untrusted profile 的 bwrap 需要显式 workdir 且只绑定工作区，
        # 用它创建尚不存在的用户目录会失败；这里用 off 模式。
        provision_settings = settings.model_copy(
            update={"shell_sandbox": "off", "persistent_shell": False}
        )
        self.sandbox = sandbox or build_sandbox(
            provision_settings, slots=slots
        )
        self.multi_user = (
            bool(multi_user)
            if multi_user is not None
            else bool(settings.web_auth_tokens or settings.web_auth_tokens_file)
        )
        self._tenants: dict[str, WebTenant] = {}
        self._lock = threading.Lock()
        self._home: str | None = None

    @property
    def home(self) -> str:
        if self._home is None:
            self._home = self.sandbox.home()
        return self._home

    @property
    def data_root(self) -> Path:
        configured = str(self.settings.web_data_root or "").strip()
        return (
            Path(configured)
            if configured
            else Path.cwd() / ".agent" / "web" / "users"
        )

    @property
    def workspaces_root(self) -> str:
        """工作区注册表的默认根（RFC-0001）。

        `AGENT_WORKSPACE_ROOT` 为空时回退到这里；调用方仍应先读显式配置。
        """
        return self._workspaces_root()

    def _workspaces_root(self) -> str:
        configured = str(self.settings.web_workspaces_root or "").strip()
        if configured:
            return normalize_root(configured)
        return f"{self.home}/agent-ws/users"

    def _memory_root(self) -> str:
        configured = str(self.settings.web_memory_root or "").strip()
        if configured:
            return normalize_root(configured)
        return f"{self.home}/agent-memory"

    def _library_root(self) -> str:
        configured = str(self.settings.web_library_root or "").strip()
        if configured:
            return normalize_root(configured)
        return f"{self.home}/agent-library"

    @staticmethod
    def _safe_user(user: str) -> str:
        text = str(user or "").strip()
        if not _USER_RE.match(text) or text in {".", ".."}:
            raise TenantError(f"非法用户名：{user!r}")
        return text

    def _ensure_dir(self, path: str, root: str) -> None:
        """创建租户目录（宿主管理动作，不执行模型意图）。

        走 `SandboxFs.mkdir` 而不是自己拼 `mkdir -p`：这是 A2 侧最后一处直接
        跑命令的地方之一（TD-034）。`root` 是允许范围的上界 —— 路径本来就由
        `ensure_inside` 推出，这里再让 A4 按同样的根复核一次，避免"上游算对了、
        下游照抄"的假设。
        """
        try:
            SandboxFs(self.sandbox, root).mkdir(path)
        except (SandboxFsError, SandboxPathError) as exc:
            raise TenantError(f"无法创建目录 {path}：{exc}") from exc

    def resolve_workspace(self, user: str, requested: str = "") -> str:
        """把会话请求里的工作区解析成允许的绝对路径。

        单用户本地模式允许用户选择任意绝对路径（GUI 里还会有显式确认）；
        多用户模式只允许落在该用户**自己的**目录之下。

        注意边界是 `workspaces_root/<user>` 而不是 `workspaces_root`：
        审计、checkpoint、记忆各自按用户隔离，但**工作区文件不隔离**，
        只校验到全局根会让 A 传 `.../users/B` 直接拿到 B 工作区的读写执行权。
        """
        tenant = self.for_user(user)
        text = str(requested or "").strip()
        if not text:
            return tenant.workspace
        if not self.multi_user:
            return normalize_root(text)
        try:
            return ensure_inside(text, tenant.workspace)
        except PathGuardError as exc:
            raise TenantError(
                f"工作区不在 {tenant.user} 的目录内：{exc}"
            ) from exc

    def for_user(self, user: str, *, org: str = "") -> WebTenant:
        safe = self._safe_user(user)
        with self._lock:
            cached = self._tenants.get(safe)
            if cached is not None:
                return cached

        # 未启用认证时是单用户本地模式：沿用原有路径，不强制加 user 子目录。
        if not self.multi_user:
            workspace = normalize_root(
                str(self.settings.wsl_workspace or f"{self.home}/agent-ws")
            )
            tenant = WebTenant(
                user=safe,
                multi_user=False,
                workspace=workspace,
                settings=self.settings,
                memory_dir=str(self.settings.memory_root or ""),
                library_dir=str(self.settings.library_root or ""),
                org=str(org or ""),
            )
        else:
            workspaces_root = self._workspaces_root()
            memory_root = self._memory_root()
            library_root = self._library_root()
            workspace = ensure_inside(
                f"{workspaces_root}/{safe}", workspaces_root
            )
            memory_dir = ensure_inside(f"{memory_root}/{safe}", memory_root)
            library_dir = ensure_inside(
                f"{library_root}/{safe}", library_root
            )
            self._ensure_dir(workspace, workspaces_root)
            self._ensure_dir(memory_dir, memory_root)
            self._ensure_dir(library_dir, library_root)

            data_dir = self.data_root / safe
            data_dir.mkdir(parents=True, exist_ok=True)
            tenant_settings = self.settings.model_copy(
                update={
                    "wsl_workspace": workspace,
                    "audit_dir": str(data_dir / "audit"),
                    "checkpoint_path": str(data_dir / "checkpoints.sqlite"),
                    "steering_dir": str(data_dir / "steering"),
                    "memory_root": memory_dir,
                    # 全局记忆也限制在租户目录内，不能跨用户共享。
                    "memory_global_root": f"{memory_dir}/global",
                    # 资料库按用户隔离，跨工作区保留。
                    "library_root": library_dir,
                }
            )
            tenant = WebTenant(
                user=safe,
                multi_user=True,
                workspace=workspace,
                settings=tenant_settings,
                data_dir=data_dir,
                memory_dir=memory_dir,
                library_dir=library_dir,
                org=str(org or ""),
            )

        with self._lock:
            self._tenants[safe] = tenant
        return tenant
