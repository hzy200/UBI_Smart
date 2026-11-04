"""A2 ↔ A3 的共享协议对象（Phase P1）。

这些 DTO 不属于 A2 的客户端实现，也不属于 A3 的 HTTP 层；放在中立模块，
让 control 与 agent_server 只依赖协议本身。

- `AgentContext` / `AgentRunRequest`：A2 发给 A3 的运行上下文；
- `AgentMeta` / `Compatibility`：A2 启动时读取的 A3 版本与能力；
- `AGENT_CAPABILITIES`：A3 承诺的能力分组。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from ubismart import AGENT_API_VERSION, __version__


def sign_context(header: str, key: str) -> str:
    """对 base64 context 做 HMAC-SHA256；空 key 返回空串。"""
    if not key:
        return ""
    return hmac.new(
        key.encode("utf-8"), header.encode("ascii"), hashlib.sha256
    ).hexdigest()


def verify_context(
    header: str, signature: str, key: str, *, required: bool = False
) -> bool:
    """校验 A2 传来的 `X-Agent-Context`。

    `key` 为空意味着没启用 HMAC。`required=False` 时保持旧行为（只依赖内部
    Bearer token，兼容未升级的部署）；`required=True` 时**空 key 一律拒绝** ——
    否则任何拿到内部 token 的人都能伪造 tenant / workspace / a2l_id。

    拆分部署与非 loopback 绑定必须传 `required=True`。
    """
    if not key:
        return not required
    if not signature:
        return False
    return hmac.compare_digest(
        sign_context(header, key), str(signature)
    )

AGENT_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "run_lifecycle": (
        "runs",
        "stream",
        "resume",
        "resume_steering",
        "steer",
        "approvals",
        "status",
        "history",
        "compact",
    ),
    "workspace": (
        "files",
        "file",
        "file_raw",
        "file_document",
        "git",
        "project",
        "snapshots",
        "diff",
        "restore",
        "uploads",
    ),
    "state": (
        "run_started",
        "checkpoints",
        "fork",
        "rewind",
        "memory",
        "audit",
        "sessions",
        "recovery",
    ),
    "execution": (
        "run",
        "read_file",
        "write_file",
        "list_dir",
        "stat",
        "git",
        "snapshot",
        "restore",
        "worktree_create",
        "worktree_remove",
        "patch_apply",
    ),
}


@dataclass(slots=True)
class AgentContext:
    tenant: str = "local"
    user: str = "local"
    workspace: str = ""
    # RFC-0001：注册表开启时由 A2 绑定；A3 只用于审计与兼容，不据此放行路径。
    workspace_id: str = ""
    workspace_kind: str = "sandbox"
    a2l_id: str = ""
    root_handle: str = ""
    # 每用户跨工作区资料库根目录（WSL 绝对路径）；空 = 由 A3 按沙箱家目录推导。
    library_root: str = ""
    access_mode: str = "read_only"
    model: str = ""
    provider: str = ""
    allow_write: bool = False
    # A2 持有的 provider 密钥只在运行请求里短期注入 A3 内存，不落盘。
    api_keys: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.api_keys is None:
            self.api_keys = {}

    def to_header(self) -> str:
        raw = json.dumps(asdict(self), ensure_ascii=False)
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @classmethod
    def from_header(cls, value: str) -> AgentContext:
        raw = base64.urlsafe_b64decode(str(value).encode("ascii")).decode(
            "utf-8"
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Agent context 不是 JSON 对象")
        return cls(
            tenant=str(data.get("tenant") or "local"),
            user=str(data.get("user") or "local"),
            workspace=str(data.get("workspace") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            workspace_kind=str(data.get("workspace_kind") or "sandbox"),
            a2l_id=str(data.get("a2l_id") or ""),
            root_handle=str(data.get("root_handle") or ""),
            library_root=str(data.get("library_root") or ""),
            access_mode=str(data.get("access_mode") or "read_only"),
            model=str(data.get("model") or ""),
            provider=str(data.get("provider") or ""),
            allow_write=bool(data.get("allow_write", False)),
            api_keys={
                str(name): str(key)
                for name, key in (data.get("api_keys") or {}).items()
                if name and key
            }
            if isinstance(data.get("api_keys"), dict)
            else {},
        )


@dataclass(slots=True)
class AgentRunRequest:
    context: AgentContext
    thread_id: str
    prompt: str


@dataclass(frozen=True)
class AgentMeta:
    version: str = ""
    api_version: int = 0
    service: str = "agent-server"
    execution_backend: str = ""
    capabilities: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> AgentMeta:
        if not isinstance(data, dict):
            raise ValueError("Agent meta 不是 JSON 对象")
        try:
            api_version = int(data.get("api_version") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("api_version 不是整数") from exc
        raw_caps = data.get("capabilities") or {}
        capabilities: dict[str, list[str]] = {}
        if isinstance(raw_caps, dict):
            for group, items in raw_caps.items():
                if isinstance(items, (list, tuple)):
                    capabilities[str(group)] = [str(item) for item in items]
        return cls(
            version=str(data.get("version") or ""),
            api_version=api_version,
            service=str(data.get("service") or "agent-server"),
            execution_backend=str(data.get("execution_backend") or ""),
            capabilities=capabilities,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "version": self.version,
            "api_version": self.api_version,
            "execution_backend": self.execution_backend,
            "capabilities": self.capabilities,
        }

    def has(self, group: str, item: str) -> bool:
        return item in self.capabilities.get(group, ())


@dataclass(frozen=True)
class Compatibility:
    ok: bool
    reason: str = ""
    warning: str = ""


def check_compatibility(
    meta: AgentMeta, *, expected_api: int = AGENT_API_VERSION
) -> Compatibility:
    """版本兼容判断：协议主版本必须一致，包版本不同只警告。"""
    if meta.api_version != expected_api:
        return Compatibility(
            ok=False,
            reason=(
                f"Agent 协议版本 {meta.api_version or '未知'} 与 "
                f"控制面要求的 {expected_api} 不一致"
            ),
        )
    if meta.version and meta.version != __version__:
        return Compatibility(
            ok=True,
            warning=(
                f"Agent 版本 {meta.version} 与控制面 {__version__} 不同，"
                "建议同步升级两侧"
            ),
        )
    return Compatibility(ok=True)
