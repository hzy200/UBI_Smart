"""Phase 1 只读能力：在本地策略/路径守卫之后执行。

它是 server 的 handler 实现，不直接暴露给 A3；A3 只能通过协议帧到达这里。
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.paths import HostPathError, HostPathPolicy
from coding_agent.host_broker.policy import GrantStore, HostPolicy
from coding_agent.host_broker.protocol import (
    CONFLICT,
    DENIED_SCOPE,
    DENIED_VERB,
    EXEC_UNAVAILABLE,
    FILE_NOT_FOUND,
    FILE_TOO_LARGE,
    GRANT_EXPIRED,
    GRANT_REQUIRED,
    GRANT_REVOKED,
    INVALID_REQUEST,
    OP_NOT_ENABLED_IN_PHASE1,
    SENSITIVE_ROOT,
    TARGET_KIND_MISMATCH,
    TOOLCHAIN_UNAVAILABLE,
    WORKSPACE_HANDLE_UNKNOWN,
    Frame,
    make_response,
)
from coding_agent.host_broker.snapshots import (
    HostSnapshotError,
    HostSnapshotStore,
    atomic_write,
)
from coding_agent.host_broker.toolchain import ToolchainRegistry
from coding_agent.host_broker.workspace_registry import WorkspaceRegistrar
from coding_agent.security.secrets import redact_secrets

_SENSITIVE_ENV = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE|SESSION",
    re.IGNORECASE,
)

DEFAULT_MAX_READ_BYTES = 256 * 1024
# 单帧上限是 1 MiB；读大文件必须走 host.read_chunk。
MAX_READ_BYTES = 512 * 1024
MAX_ENV_NAMES = 50
CHUNK_BYTES = 256 * 1024
MAX_CHUNK_BYTES = 512 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UPLOAD_TTL_S = 600.0


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
        appcontainer: Any | None = None,
        isolation: dict[str, Any] | None = None,
        snapshots: HostSnapshotStore | None = None,
        allow_write: bool = False,
        allow_exec: bool = False,
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
        self.appcontainer = appcontainer
        self.isolation = dict(isolation or {})
        self.snapshots = snapshots
        self.allow_write = bool(allow_write)
        self.allow_exec = bool(allow_exec)
        self._uploads: dict[str, dict[str, Any]] = {}
        self._upload_lock = threading.Lock()

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
                    "isolation": dict(self.isolation),
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
        if frame.kind == "host.read_chunk":
            return self._read_chunk(frame)
        if frame.kind == "host.write_begin":
            return self._write_begin(frame)
        if frame.kind == "host.write_chunk":
            return self._write_chunk(frame)
        if frame.kind == "host.write_commit":
            return self._write_commit(frame)
        if frame.kind == "host.write_abort":
            return self._write_abort(frame)
        if frame.kind == "host.write_text":
            return self._write_text(frame)
        if frame.kind == "host.write_many_commit":
            return self._write_many_commit(frame)
        if frame.kind == "host.delete":
            return self._delete(frame)
        if frame.kind == "host.mkdir":
            return self._mkdir(frame)
        if frame.kind == "host.snapshot_save":
            return self._snapshot_save(frame)
        if frame.kind == "host.snapshot_list":
            return self._snapshot_list(frame)
        if frame.kind == "host.restore":
            return self._restore(frame)
        if frame.kind == "host.exec":
            return self._exec(frame)
        if frame.kind == "host.install":
            return self._install(frame)
        raise BrokerOpError(
            OP_NOT_ENABLED_IN_PHASE1, f"未开放：{frame.kind}"
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

    def _resolve_path(
        self,
        frame: Frame,
        relative: str,
        verb: str,
        *,
        prompt_reason: str = "",
    ) -> tuple[str, str]:
        _, root = self._workspace(frame)
        try:
            path = self.path_policy.resolve_child(root, relative)
        except HostPathError as exc:
            raise BrokerOpError(exc.code, exc.message) from exc
        payload = dict(frame.payload or {})
        grant = self.grants.get(frame.grant_id)
        decision = self.policy.check(
            path,
            verb,
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
                    "verbs": [verb],
                    "ttl_s": 1800,
                    "reason": prompt_reason
                    or f"{frame.kind} {relative}",
                    "sensitive": bool(decision.sensitive),
                    "one_shot": verb in {"exec", "install"},
                }
            raise BrokerOpError(decision.code, decision.reason, data=data)
        return root, path

    def _resolve(self, frame: Frame) -> tuple[str, str]:
        payload = dict(frame.payload or {})
        relative = str(payload.get("path") or ".")
        return self._resolve_path(frame, relative, "read")

    def _require_verb(self, frame: Frame, verb: str) -> str:
        """根级 op（exec/install/snapshot_list）的授权检查。"""
        _, root = self._workspace(frame)
        grant = self.grants.get(frame.grant_id)
        decision = self.policy.check(
            root,
            verb,
            grant,
            sensitive_ack=bool(frame.payload.get("sensitive_ack", False)),
        )
        if not decision.allowed:
            data: dict[str, Any] = {}
            if decision.code in {
                GRANT_REQUIRED,
                GRANT_EXPIRED,
                GRANT_REVOKED,
                DENIED_SCOPE,
                DENIED_VERB,
                SENSITIVE_ROOT,
            }:
                data = {
                    "scope": [root],
                    "verbs": [verb],
                    "ttl_s": 1800,
                    "reason": f"{frame.kind} workspace",
                    "sensitive": bool(decision.sensitive),
                    "one_shot": verb in {"exec", "install"},
                }
            raise BrokerOpError(decision.code, decision.reason, data=data)
        return root

    def _helper_result(self, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("ok"):
            return result
        code = str(result.get("code") or INVALID_REQUEST)
        if code == "ACCESS_DENIED":
            code = INVALID_REQUEST
        raise BrokerOpError(code, str(result.get("error") or "读取失败"))

    def _list_dir(self, frame: Frame) -> Frame:
        root, path = self._resolve(frame)
        payload = dict(frame.payload or {})
        limit = int(payload.get("limit", 200))
        limit = max(1, min(limit, 1000))
        if self.appcontainer is not None:
            result = self._helper_result(
                self.appcontainer.call(
                    "list", {"path": path, "limit": limit}
                )
            )
            return make_response(
                frame,
                data={
                    "entries": [
                        {
                            "kind": str(item.get("kind") or "f"),
                            "path": self.path_policy.relative_to_root(
                                root, os.path.join(path, str(item["name"]))
                            ),
                        }
                        for item in result.get("entries", [])
                    ]
                },
            )
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
        if self.appcontainer is not None:
            result = self._helper_result(
                self.appcontainer.call(
                    "find",
                    {"path": path, "pattern": pattern, "limit": limit},
                )
            )
            return make_response(
                frame, data={"files": list(result.get("files", []))}
            )
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
        if self.appcontainer is not None:
            result = self._helper_result(
                self.appcontainer.call("stat", {"path": path})
            )
            if not result.get("exists"):
                return make_response(frame, data={"exists": False})
            return make_response(
                frame,
                data={
                    "exists": True,
                    "is_file": bool(result.get("is_file")),
                    "is_dir": bool(result.get("is_dir")),
                    "size": int(result.get("size", 0)),
                    "mtime": float(result.get("mtime", 0)),
                    "path": self.path_policy.relative_to_root(root, path),
                },
            )
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
        if self.appcontainer is not None:
            result = self._helper_result(
                self.appcontainer.call(
                    "read", {"path": path, "max_bytes": max_bytes}
                )
            )
            text = str(result.get("text") or "")
            return make_response(
                frame,
                data={
                    "path": self.path_policy.relative_to_root(root, path),
                    "text": text,
                    "bytes": len(text.encode("utf-8")),
                },
            )
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

    # ------------------------------------------------------------------
    # 写：分块上传、原子提交、删除/建目录
    # ------------------------------------------------------------------
    def _require_write(self) -> None:
        if not self.allow_write:
            raise BrokerOpError(OP_NOT_ENABLED_IN_PHASE1, "host 写未启用")

    @staticmethod
    def _sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _current_sha(self, path: str) -> str:
        try:
            with open(path, "rb") as handle:
                return self._sha(handle.read())
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"无法读取当前文件：{exc}") from exc

    def _check_expected(self, path: str, expected: str | None) -> None:
        if expected is None:
            return
        current = self._current_sha(path)
        if current != expected:
            raise BrokerOpError(
                CONFLICT,
                "文件在读取后已被修改，拒绝覆盖",
                data={"current_sha256": current},
            )

    def _save_snapshot(self, root: str, path: str) -> str:
        if self.snapshots is None:
            return ""
        try:
            return self.snapshots.save(
                root, path, existed=os.path.isfile(path)
            )
        except HostSnapshotError as exc:
            raise BrokerOpError(
                INVALID_REQUEST, f"写入前留底失败：{exc}"
            ) from exc

    def _cleanup_uploads(self) -> None:
        now = time.monotonic()
        with self._upload_lock:
            expired = [
                key
                for key, item in self._uploads.items()
                if now - float(item["created"]) > UPLOAD_TTL_S
            ]
            for key in expired:
                self._uploads.pop(key, None)

    def _read_chunk(self, frame: Frame) -> Frame:
        _, path = self._resolve(frame)
        payload = dict(frame.payload or {})
        offset = int(payload.get("offset", 0))
        length = int(payload.get("length", CHUNK_BYTES))
        if offset < 0 or length <= 0 or length > MAX_CHUNK_BYTES:
            raise BrokerOpError(INVALID_REQUEST, "非法的 offset/length")
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(length)
        except FileNotFoundError as exc:
            raise BrokerOpError(FILE_NOT_FOUND, f"文件不存在：{path}") from exc
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"读取失败：{exc}") from exc
        return make_response(
            frame,
            data={
                "data_b64": base64.b64encode(data).decode("ascii"),
                "offset": offset,
                "next": offset + len(data),
                "size": size,
                "eof": offset + len(data) >= size,
            },
        )

    def _write_begin(self, frame: Frame) -> Frame:
        self._require_write()
        payload = dict(frame.payload or {})
        relative = str(payload.get("path") or "")
        if not relative:
            raise BrokerOpError(INVALID_REQUEST, "path 不能为空")
        root, path = self._resolve_path(frame, relative, "write")
        expected = payload.get("expected_sha256")
        if expected is not None:
            expected = str(expected)
            self._check_expected(path, expected)
        upload_id = uuid.uuid4().hex
        self._cleanup_uploads()
        with self._upload_lock:
            if len(self._uploads) >= 32:
                raise BrokerOpError(INVALID_REQUEST, "上传会话过多")
            self._uploads[upload_id] = {
                "root": root,
                "path": path,
                "relative": relative,
                "grant_id": frame.grant_id,
                "expected": expected,
                "data": bytearray(),
                "created": time.monotonic(),
            }
        return make_response(
            frame,
            data={
                "upload_id": upload_id,
                "exists": os.path.isfile(path),
                "sha256": self._current_sha(path),
            },
        )

    def _write_chunk(self, frame: Frame) -> Frame:
        self._require_write()
        payload = dict(frame.payload or {})
        upload_id = str(payload.get("upload_id") or "")
        offset = int(payload.get("offset", -1))
        try:
            data = base64.b64decode(
                str(payload.get("data_b64") or ""), validate=True
            )
        except (ValueError, TypeError) as exc:
            raise BrokerOpError(INVALID_REQUEST, "data_b64 非法") from exc
        if len(data) > MAX_CHUNK_BYTES:
            raise BrokerOpError(INVALID_REQUEST, "分块过大")
        with self._upload_lock:
            upload = self._uploads.get(upload_id)
            if upload is None:
                raise BrokerOpError(INVALID_REQUEST, "未知 upload_id")
            if offset != len(upload["data"]):
                raise BrokerOpError(
                    CONFLICT,
                    "分块 offset 不连续",
                    data={"expected_offset": len(upload["data"])},
                )
            if len(upload["data"]) + len(data) > MAX_UPLOAD_BYTES:
                raise BrokerOpError(FILE_TOO_LARGE, "上传超过上限")
            upload["data"].extend(data)
            total = len(upload["data"])
        return make_response(frame, data={"offset": total})

    def _write_commit(self, frame: Frame) -> Frame:
        self._require_write()
        upload_id = str(frame.payload.get("upload_id") or "")
        with self._upload_lock:
            upload = self._uploads.pop(upload_id, None)
        if upload is None:
            raise BrokerOpError(INVALID_REQUEST, "未知 upload_id")
        # 提交时重新判定，防止 begin 之后 grant 被撤销或路径变化。
        root, path = self._resolve_path(
            frame, str(upload["relative"]), "write"
        )
        expected = upload.get("expected")
        # `BrokerOpError` 原样上抛；包一层 try 只会让栈更厚。
        self._check_expected(path, expected)
        data = bytes(upload["data"])
        snapshot_id = self._save_snapshot(root, path)
        try:
            atomic_write(path, data)
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"写入失败：{exc}") from exc
        return make_response(
            frame,
            data={
                "path": self.path_policy.relative_to_root(root, path),
                "bytes": len(data),
                "sha256": self._sha(data),
                "snapshot_id": snapshot_id,
            },
        )

    def _write_abort(self, frame: Frame) -> Frame:
        upload_id = str(frame.payload.get("upload_id") or "")
        with self._upload_lock:
            self._uploads.pop(upload_id, None)
        return make_response(frame, data={"aborted": True})

    def _write_text(self, frame: Frame) -> Frame:
        """小文件快捷路径；大文件仍应使用 begin/chunk/commit。"""
        self._require_write()
        payload = dict(frame.payload or {})
        relative = str(payload.get("path") or "")
        if not relative:
            raise BrokerOpError(INVALID_REQUEST, "path 不能为空")
        text = str(payload.get("text") or "")
        data = text.encode("utf-8")
        if len(data) > MAX_CHUNK_BYTES:
            raise BrokerOpError(FILE_TOO_LARGE, "请使用分块写入")
        root, path = self._resolve_path(frame, relative, "write")
        expected = payload.get("expected_sha256")
        if expected is not None:
            self._check_expected(path, str(expected))
        snapshot_id = self._save_snapshot(root, path)
        try:
            atomic_write(path, data)
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"写入失败：{exc}") from exc
        return make_response(
            frame,
            data={
                "path": self.path_policy.relative_to_root(root, path),
                "bytes": len(data),
                "sha256": self._sha(data),
                "snapshot_id": snapshot_id,
            },
        )

    def _write_many_commit(self, frame: Frame) -> Frame:
        self._require_write()
        ids = frame.payload.get("uploads")
        if not isinstance(ids, list) or not ids:
            raise BrokerOpError(INVALID_REQUEST, "uploads 必须是非空数组")
        with self._upload_lock:
            uploads = []
            for upload_id in ids:
                upload = self._uploads.pop(str(upload_id), None)
                if upload is None:
                    raise BrokerOpError(
                        INVALID_REQUEST, f"未知 upload_id：{upload_id}"
                    )
                uploads.append(upload)
        checked: list[tuple[str, str, bytes, str]] = []
        for upload in uploads:
            root, path = self._resolve_path(
                frame, str(upload["relative"]), "write"
            )
            self._check_expected(path, upload.get("expected"))
            checked.append(
                (root, path, bytes(upload["data"]), upload["relative"])
            )
        snapshots: list[tuple[str, str]] = []
        written: list[tuple[str, str, bytes]] = []
        try:
            for root, path, data, _relative in checked:
                snapshot_id = self._save_snapshot(root, path)
                snapshots.append((root, snapshot_id))
                atomic_write(path, data)
                written.append((root, path, data))
        except Exception as exc:  # noqa: BLE001 - 尽力回滚已写文件
            for root, snapshot_id in reversed(snapshots):
                if snapshot_id and self.snapshots is not None:
                    try:
                        self.snapshots.restore(root, snapshot_id)
                    except Exception:  # noqa: BLE001
                        pass
            raise BrokerOpError(INVALID_REQUEST, f"批量写入失败：{exc}") from exc
        return make_response(
            frame,
            data={
                "written": [
                    {
                        "path": self.path_policy.relative_to_root(root, path),
                        "bytes": len(data),
                        "snapshot_id": snapshot_id,
                    }
                    for (root, path, data), (_, snapshot_id) in zip(
                        written, snapshots, strict=True
                    )
                ]
            },
        )

    def _delete(self, frame: Frame) -> Frame:
        self._require_write()
        root, path = self._resolve_path(
            frame, str(frame.payload.get("path") or ""), "write"
        )
        relative = self.path_policy.relative_to_root(root, path)
        if not os.path.exists(path):
            return make_response(
                frame, data={"deleted": False, "path": relative}
            )
        if not os.path.isfile(path):
            raise BrokerOpError(INVALID_REQUEST, "只允许删除普通文件")
        snapshot_id = self._save_snapshot(root, path)
        try:
            os.remove(path)
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"删除失败：{exc}") from exc
        return make_response(
            frame,
            data={
                "deleted": True,
                "path": relative,
                "snapshot_id": snapshot_id,
            },
        )

    def _mkdir(self, frame: Frame) -> Frame:
        self._require_write()
        root, path = self._resolve_path(
            frame, str(frame.payload.get("path") or ""), "write"
        )
        if os.path.exists(path) and not os.path.isdir(path):
            raise BrokerOpError(INVALID_REQUEST, "目标已存在且不是目录")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise BrokerOpError(INVALID_REQUEST, f"创建目录失败：{exc}") from exc
        return make_response(
            frame,
            data={"path": self.path_policy.relative_to_root(root, path)},
        )

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def _snapshot_store(self) -> HostSnapshotStore:
        if self.snapshots is None:
            raise BrokerOpError(INVALID_REQUEST, "宿主快照未配置")
        return self.snapshots

    def _snapshot_save(self, frame: Frame) -> Frame:
        self._require_write()
        root, path = self._resolve_path(
            frame, str(frame.payload.get("path") or ""), "write"
        )
        if os.path.isdir(path):
            raise BrokerOpError(INVALID_REQUEST, "不为目录留底")
        snapshot_id = self._save_snapshot(root, path)
        return make_response(
            frame,
            data={
                "snapshot_id": snapshot_id,
                "path": self.path_policy.relative_to_root(root, path),
            },
        )

    def _snapshot_list(self, frame: Frame) -> Frame:
        root = self._require_verb(frame, "read")
        payload = dict(frame.payload or {})
        limit = max(1, min(int(payload.get("limit", 50)), 1000))
        filter_path = str(payload.get("path") or "")
        wanted_rel = ""
        if filter_path:
            _, wanted = self._resolve_path(frame, filter_path, "read")
            wanted_rel = self.path_policy.relative_to_root(root, wanted)
        entries = []
        for entry in self._snapshot_store().list(root, limit=0):
            if wanted_rel and entry.path != wanted_rel:
                continue
            entries.append(
                {
                    "snapshot_id": entry.snapshot_id,
                    "path": entry.path,
                    "absent": self._snapshot_store().was_absent(root, entry),
                }
            )
            if len(entries) >= limit:
                break
        return make_response(frame, data={"entries": entries})

    def _restore(self, frame: Frame) -> Frame:
        self._require_write()
        payload = dict(frame.payload or {})
        snapshot_id = str(payload.get("snapshot_id") or "")
        if not snapshot_id:
            raise BrokerOpError(INVALID_REQUEST, "snapshot_id 不能为空")
        store = self._snapshot_store()
        _, root = self._workspace(frame)
        explicit = str(payload.get("path") or "")
        if explicit:
            _, wanted = self._resolve_path(frame, explicit, "write")
            rel = self.path_policy.relative_to_root(root, wanted)
            entry = store.find(root, snapshot_id, rel)
        else:
            entry = store.find(root, snapshot_id)
        if entry is None:
            raise BrokerOpError(FILE_NOT_FOUND, "找不到快照")
        # 按快照里的相对路径重新做一次写授权。
        self._resolve_path(frame, entry.path, "write")
        result = store.restore(root, entry.snapshot_id, entry.path)
        if not result.ok:
            raise BrokerOpError(INVALID_REQUEST, result.message)
        return make_response(
            frame,
            data={
                "snapshot_id": result.snapshot_id,
                "path": result.path,
                "undo_snapshot_id": result.undo_snapshot_id,
                "added": result.added,
                "removed": result.removed,
                "diff": result.diff,
                "message": result.message,
            },
        )

    # ------------------------------------------------------------------
    # 执行与安装
    # ------------------------------------------------------------------
    def _exec(self, frame: Frame) -> Frame:
        if not self.allow_exec:
            raise BrokerOpError(EXEC_UNAVAILABLE, "host.exec 未启用")
        root = self._require_verb(frame, "exec")
        if self.security_tier != "appcontainer" or self.appcontainer is None:
            raise BrokerOpError(
                EXEC_UNAVAILABLE,
                "host.exec 只在 AppContainer 档可用",
            )
        payload = dict(frame.payload or {})
        toolchain_id = str(payload.get("toolchain") or "")
        ref = self.toolchains.get(toolchain_id)
        if ref is None or not ref.commands:
            raise BrokerOpError(
                TOOLCHAIN_UNAVAILABLE, f"未注册工具链：{toolchain_id}"
            )
        from coding_agent.host_broker.exec import run_host_exec

        output_roots: list[str] = []
        for relative in ref.output_roots:
            # output_roots 属于 exec 授权范围，不要求额外的 write verb。
            _, path = self._resolve_path(frame, relative, "exec")
            output_roots.append(path)
        result = run_host_exec(
            runner=self.appcontainer,
            toolchain=ref,
            registry=self.toolchains,
            toolchain_id=toolchain_id,
            payload=payload,
            workspace_root=root,
            output_roots=output_roots,
            path_policy=self.path_policy,
        )
        return make_response(frame, data=result)

    def _install(self, frame: Frame) -> Frame:
        self._require_verb(frame, "install")
        payload = dict(frame.payload or {})
        manager = str(payload.get("manager") or "").strip()
        command = str(payload.get("command") or "").strip()
        if not manager or not command:
            raise BrokerOpError(
                INVALID_REQUEST, "install 需要 manager 与 command"
            )
        if len(command) > 4000:
            raise BrokerOpError(INVALID_REQUEST, "安装命令过长")
        return make_response(
            frame,
            data={
                "manual": True,
                "executed": False,
                "manager": manager,
                "command": redact_secrets(command),
                "url": str(payload.get("url") or ""),
                "requires_admin": bool(payload.get("requires_admin", False)),
                "reason": str(payload.get("reason") or ""),
                "message": (
                    "安装属于 L4 环境变更，A2L 不会自动执行；"
                    "请在本机确认后手动运行上面的命令或打开下载页。"
                ),
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
