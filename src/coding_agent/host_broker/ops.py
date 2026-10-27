"""Phase 1 只读能力：在本地策略/路径守卫之后执行。

它是 server 的 handler 实现，不直接暴露给 A3；A3 只能通过协议帧到达这里。
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping

from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.paths import HostPathError, HostPathPolicy
from coding_agent.host_broker.policy import GrantStore, HostPolicy
from coding_agent.host_broker.protocol import (
    DENIED_SCOPE,
    DENIED_VERB,
    FILE_NOT_FOUND,
    FILE_TOO_LARGE,
    GRANT_EXPIRED,
    GRANT_REQUIRED,
    GRANT_REVOKED,
    INVALID_REQUEST,
    OP_NOT_ENABLED_IN_PHASE1,
    SENSITIVE_ROOT,
    TARGET_KIND_MISMATCH,
    WORKSPACE_HANDLE_UNKNOWN,
    Frame,
    make_response,
)
from coding_agent.host_broker.toolchain import ToolchainRegistry
from coding_agent.host_broker.workspace_registry import WorkspaceRegistrar
from coding_agent.security.secrets import redact_secrets

_SENSITIVE_ENV = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE|SESSION",
    re.IGNORECASE,
)

DEFAULT_MAX_READ_BYTES = 256 * 1024
MAX_READ_BYTES = 2 * 1024 * 1024
MAX_ENV_NAMES = 50


class HostReadOnlyOps:
    def __init__(
        self,
        *,
        policy: HostPolicy,
        path_policy: HostPathPolicy,
        grants: GrantStore,
        toolchains: ToolchainRegistry,
        workspace_roots: Mapping[str, str],
        broker_id: str = "",
        security_tier: str = "lowpriv",
        threat_model: str = "T1",
        workspace_registrar: WorkspaceRegistrar | None = None,
    ) -> None:
        self.policy = policy
        self.path_policy = path_policy
        self.grants = grants
        self.toolchains = toolchains
        self.workspace_roots = workspace_roots
        self.broker_id = str(broker_id)
        self.security_tier = str(security_tier)
        self.threat_model = str(threat_model)
        self.workspace_registrar = workspace_registrar

    async def handle(self, frame: Frame) -> Frame:
        if frame.kind == "host.workspace.register":
            if self.workspace_registrar is None:
                raise BrokerOpError(
                    OP_NOT_ENABLED_IN_PHASE1, "未配置本地工作区选择器"
                )
            data = await self.workspace_registrar.register()
            return make_response(frame, data=data)
        if frame.kind == "host.health":
            return make_response(
                frame,
                data={
                    "broker_id": self.broker_id,
                    "security_tier": self.security_tier,
                    "threat_model": self.threat_model,
                    "toolchains": self.toolchains.summary()["toolchains"],
                },
            )
        if frame.kind == "host.toolchains":
            return make_response(frame, data=self.toolchains.summary())
        if frame.kind == "host.env.inspect":
            return self._env_inspect(frame)
        if frame.kind == "host.list_dir":
            return self._list_dir(frame)
        if frame.kind == "host.find_files":
            return self._find_files(frame)
        if frame.kind == "host.stat":
            return self._stat(frame)
        if frame.kind == "host.read_text":
            return self._read_text(frame)
        raise BrokerOpError(
            OP_NOT_ENABLED_IN_PHASE1, f"Phase 1 未开放：{frame.kind}"
        )

    def _workspace(self, frame: Frame) -> tuple[str, str]:
        workspace = frame.workspace or {}
        if str(workspace.get("kind") or "host") != "host":
            raise BrokerOpError(
                TARGET_KIND_MISMATCH, "A2L 只处理 host 工作区"
            )
        workspace_id = str(workspace.get("workspace_id") or "")
        root = self.workspace_roots.get(workspace_id)
        if not root:
            raise BrokerOpError(
                WORKSPACE_HANDLE_UNKNOWN,
                f"未知 workspace_id：{workspace_id or '<empty>'}",
            )
        return workspace_id, root

    def _resolve(self, frame: Frame) -> tuple[str, str]:
        _, root = self._workspace(frame)
        payload = dict(frame.payload or {})
        relative = str(payload.get("path") or ".")
        try:
            path = self.path_policy.resolve_child(root, relative)
        except HostPathError as exc:
            raise BrokerOpError(exc.code, exc.message) from exc
        grant = self.grants.get(frame.grant_id)
        decision = self.policy.check(
            path,
            "read",
            grant,
            sensitive_ack=bool(payload.get("sensitive_ack", False)),
        )
        if not decision.allowed:
            grant_codes = {
                GRANT_REQUIRED,
                GRANT_EXPIRED,
                GRANT_REVOKED,
                DENIED_SCOPE,
                DENIED_VERB,
                SENSITIVE_ROOT,
            }
            data = {}
            if decision.code in grant_codes:
                data = {
                    "scope": [root],
                    "verbs": ["read"],
                    "ttl_s": 1800,
                    "reason": f"{frame.kind} {relative}",
                    "sensitive": bool(decision.sensitive),
                }
            raise BrokerOpError(decision.code, decision.reason, data=data)
        return root, path

    def _list_dir(self, frame: Frame) -> Frame:
        root, path = self._resolve(frame)
        payload = dict(frame.payload or {})
        limit = int(payload.get("limit", 200))
        limit = max(1, min(limit, 1000))
        entries: list[dict[str, str]] = []
        try:
            with os.scandir(path) as handle:
                rows = sorted(handle, key=lambda item: item.name.lower())
                for entry in rows[:limit]:
                    if entry.is_symlink():
                        kind = "l"
                    elif entry.is_dir(follow_symlinks=False):
                        kind = "d"
                    else:
                        kind = "f"
                    entries.append(
                        {
                            "kind": kind,
                            "path": self.path_policy.relative_to_root(
                                root, entry.path
                            ),
                        }
                    )
        except FileNotFoundError as exc:
            raise BrokerOpError(FILE_NOT_FOUND, f"目录不存在：{path}") from exc
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"无法列目录：{exc}") from exc
        return make_response(frame, data={"entries": entries})

    def _find_files(self, frame: Frame) -> Frame:
        _, path = self._resolve(frame)
        payload = dict(frame.payload or {})
        pattern = str(payload.get("pattern") or "").strip()
        if not pattern:
            raise BrokerOpError(INVALID_REQUEST, "pattern 不能为空")
        limit = max(1, min(int(payload.get("limit", 200)), 1000))
        found: list[str] = []
        stack: list[tuple[str, str, int]] = [(path, "", 0)]
        while stack and len(found) < limit:
            current, prefix, depth = stack.pop()
            if depth >= 12:
                continue
            try:
                with os.scandir(current) as handle:
                    entries = sorted(
                        handle, key=lambda item: item.name.lower()
                    )
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                rel = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, rel, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(
                        entry.name, pattern
                    ):
                        found.append(rel)
                        if len(found) >= limit:
                            break
        found.sort()
        return make_response(frame, data={"files": found[:limit]})

    def _stat(self, frame: Frame) -> Frame:
        root, path = self._resolve(frame)
        try:
            info = os.stat(path)
        except FileNotFoundError:
            return make_response(frame, data={"exists": False})
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"无法读取状态：{exc}") from exc
        return make_response(
            frame,
            data={
                "exists": True,
                "is_file": os.path.isfile(path),
                "is_dir": os.path.isdir(path),
                "size": int(info.st_size),
                "mtime": float(info.st_mtime),
                "path": self.path_policy.relative_to_root(root, path),
            },
        )

    def _read_text(self, frame: Frame) -> Frame:
        root, path = self._resolve(frame)
        payload = dict(frame.payload or {})
        max_bytes = int(payload.get("max_bytes", DEFAULT_MAX_READ_BYTES))
        max_bytes = max(1, min(max_bytes, MAX_READ_BYTES))
        try:
            size = os.path.getsize(path)
        except FileNotFoundError as exc:
            raise BrokerOpError(FILE_NOT_FOUND, f"文件不存在：{path}") from exc
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"无法读取大小：{exc}") from exc
        if size > max_bytes:
            raise BrokerOpError(
                FILE_TOO_LARGE,
                f"文件过大：{size} 字节（上限 {max_bytes}）",
            )
        try:
            with open(path, "rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"读取失败：{exc}") from exc
        if len(data) > max_bytes:
            raise BrokerOpError(
                FILE_TOO_LARGE,
                f"读取期间文件超过上限 {max_bytes} 字节",
            )
        if b"\x00" in data:
            raise BrokerOpError(INVALID_REQUEST, "二进制文件不支持读取")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BrokerOpError(
                INVALID_REQUEST, f"非 UTF-8 文本：{exc.reason}"
            ) from exc
        return make_response(
            frame,
            data={
                "path": self.path_policy.relative_to_root(root, path),
                "text": redact_secrets(text),
                "truncated": False,
            },
        )

    def _require_read_grant(self, frame: Frame) -> None:
        grant = self.grants.get(frame.grant_id)
        if grant is None:
            raise BrokerOpError(GRANT_REQUIRED, "host.env.inspect 需要授权")
        if grant.revoked:
            raise BrokerOpError(GRANT_REVOKED, "授权已撤销")
        if grant.is_expired():
            raise BrokerOpError(GRANT_EXPIRED, "授权已过期")
        if "read" not in grant.verbs:
            raise BrokerOpError(GRANT_REQUIRED, "授权不含 read")

    def _env_inspect(self, frame: Frame) -> Frame:
        self._require_read_grant(frame)
        payload = dict(frame.payload or {})
        names = payload.get("names")
        if not isinstance(names, list):
            raise BrokerOpError(INVALID_REQUEST, "names 必须是数组")
        values: dict[str, str] = {}
        for raw in names[:MAX_ENV_NAMES]:
            name = str(raw or "").strip()
            if not name or _SENSITIVE_ENV.search(name):
                continue
            value = os.environ.get(name)
            if value is not None:
                values[name] = redact_secrets(value)
        return make_response(frame, data={"values": values})
