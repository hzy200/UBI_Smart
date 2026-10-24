"""A2 ↔ A3 内部协议：版本握手与能力清单（Phase D1）。

A2 启动时读取 A3 的 `/v1/meta`，确认双方使用同一版内部协议。版本只在
破坏性变更时递增；补丁版本不同只记警告，不阻断启动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from coding_agent import AGENT_API_VERSION, __version__

# A3 对外承诺的能力分组；新增能力时同步这里与 agent_server 的路由。
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
