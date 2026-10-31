"""A2L 协议帧：版本化、可校验、与传输层解耦。

Phase 1 只做只读 op，但帧类型已覆盖 grant 闭环；未知类型/畸形帧一律拒绝。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

PROTOCOL_VERSION = 2
MAX_FRAME_BYTES = 1024 * 1024

# 错误码与 docs/PHASE_A2L.md §3.4 保持一致。
DENIED_ZX = "DENIED_ZX"
DENIED_SCOPE = "DENIED_SCOPE"
DENIED_VERB = "DENIED_VERB"
GRANT_REQUIRED = "GRANT_REQUIRED"
GRANT_PENDING = "GRANT_PENDING"
GRANT_DENIED = "GRANT_DENIED"
GRANT_EXPIRED = "GRANT_EXPIRED"
GRANT_REVOKED = "GRANT_REVOKED"
SENSITIVE_ROOT = "SENSITIVE_ROOT"
PATH_ESCAPE = "PATH_ESCAPE"
INVALID_PATH = "INVALID_PATH"
UNSUPPORTED_FILESYSTEM = "UNSUPPORTED_FILESYSTEM"
TARGET_KIND_MISMATCH = "TARGET_KIND_MISMATCH"
WORKSPACE_HANDLE_UNKNOWN = "WORKSPACE_HANDLE_UNKNOWN"
WORKSPACE_UNAVAILABLE = "WORKSPACE_UNAVAILABLE"
OP_UNKNOWN = "OP_UNKNOWN"
OP_NOT_ENABLED_IN_PHASE1 = "OP_NOT_ENABLED_IN_PHASE1"
OP_UNCERTAIN = "OP_UNCERTAIN"
CONFLICT = "CONFLICT"
EXEC_UNAVAILABLE = "EXEC_UNAVAILABLE"
INSTALL_MANUAL_ONLY = "INSTALL_MANUAL_ONLY"
INVALID_REQUEST = "INVALID_REQUEST"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
MALFORMED_FRAME = "MALFORMED_FRAME"
PROTOCOL_VERSION_ERROR = "PROTOCOL_VERSION"
TOOLCHAIN_UNAVAILABLE = "TOOLCHAIN_UNAVAILABLE"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
GATEWAY_OFFLINE = "GATEWAY_OFFLINE"
A2L_PAUSED = "A2L_PAUSED"
UNAUTHENTICATED = "UNAUTHENTICATED"
GRANT_LOCAL_ONLY = "GRANT_LOCAL_ONLY"
GRANT_RATE_LIMITED = "GRANT_RATE_LIMITED"
GRANT_USED = "GRANT_USED"
INTERNAL = "INTERNAL"

SUPPORTED_TYPES = frozenset(
    {
        "hello",
        "hello_ok",
        "heartbeat",
        "request",
        "response",
        "cancel",
        "grant.request",
        "grant.pending",
        "grant.granted",
        "grant.denied",
        "grant.revoke",
        "event",
    }
)


class ProtocolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProtocolError("MALFORMED_FRAME", f"{name} 必须是对象")
    return dict(value)


@dataclass(frozen=True, slots=True)
class Frame:
    type: str
    id: str = ""
    v: int = PROTOCOL_VERSION
    op_id: str = ""
    kind: str = ""
    grant_id: str = ""
    workspace: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    deadline_ms: int | None = None
    status: str = ""
    exit_code: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"v": self.v, "type": self.type}
        for key in ("id", "op_id", "kind", "grant_id", "status", "error_code", "error"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.workspace is not None:
            out["workspace"] = self.workspace
        if self.payload:
            out["payload"] = self.payload
        if self.deadline_ms is not None:
            out["deadline_ms"] = self.deadline_ms
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.data:
            out["data"] = self.data
        if self.meta:
            out["meta"] = self.meta
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Frame:
        if not isinstance(payload, dict):
            raise ProtocolError("MALFORMED_FRAME", "帧必须是 JSON 对象")
        frame_type = str(payload.get("type") or "").strip()
        if not frame_type:
            raise ProtocolError("MALFORMED_FRAME", "缺少 type")
        if frame_type not in SUPPORTED_TYPES:
            raise ProtocolError(OP_UNKNOWN, f"未知帧类型：{frame_type}")
        version = payload.get("v", PROTOCOL_VERSION)
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                PROTOCOL_VERSION_ERROR, f"非法协议版本：{version!r}"
            ) from exc
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                PROTOCOL_VERSION_ERROR,
                f"协议版本不匹配：收到 {version}，期望 {PROTOCOL_VERSION}",
            )
        exit_code = payload.get("exit_code")
        if exit_code is not None:
            try:
                exit_code = int(exit_code)
            except (TypeError, ValueError) as exc:
                raise ProtocolError("MALFORMED_FRAME", "exit_code 必须是整数") from exc
        deadline = payload.get("deadline_ms")
        if deadline is not None:
            try:
                deadline = int(deadline)
            except (TypeError, ValueError) as exc:
                raise ProtocolError("MALFORMED_FRAME", "deadline_ms 必须是整数") from exc
        frame = cls(
            type=frame_type,
            id=str(payload.get("id") or ""),
            v=version,
            op_id=str(payload.get("op_id") or ""),
            kind=str(payload.get("kind") or ""),
            grant_id=str(payload.get("grant_id") or ""),
            workspace=(
                _mapping(payload.get("workspace"), "workspace")
                if payload.get("workspace") is not None
                else None
            ),
            payload=_mapping(payload.get("payload"), "payload"),
            deadline_ms=deadline,
            status=str(payload.get("status") or ""),
            exit_code=exit_code,
            data=_mapping(payload.get("data"), "data"),
            error_code=str(payload.get("error_code") or ""),
            error=str(payload.get("error") or ""),
            meta=_mapping(payload.get("meta"), "meta"),
        )
        _validate(frame)
        return frame


def _validate(frame: Frame) -> None:
    if frame.type == "request":
        if not frame.id or not frame.op_id or not frame.kind:
            raise ProtocolError(
                "MALFORMED_FRAME", "request 需要 id/op_id/kind"
            )
    elif frame.type == "response":
        # hello 拒绝没有 op_id；按 id 关联请求即可。
        if not frame.id or not frame.status:
            raise ProtocolError(
                "MALFORMED_FRAME", "response 需要 id/status"
            )
    elif frame.type == "cancel":
        if not frame.op_id:
            raise ProtocolError("MALFORMED_FRAME", "cancel 需要 op_id")


def encode_frame(frame: Frame) -> str:
    raw = json.dumps(frame.to_dict(), ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolError("FRAME_TOO_LARGE", "帧超过 1 MiB")
    return raw


def decode_frame(raw: str | bytes) -> Frame:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("MALFORMED_FRAME", "帧不是 UTF-8") from exc
    if not isinstance(raw, str):
        raise ProtocolError("MALFORMED_FRAME", "帧必须是字符串或字节")
    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolError("FRAME_TOO_LARGE", "帧超过 1 MiB")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError("MALFORMED_FRAME", "JSON 解析失败") from exc
    return Frame.from_dict(payload)


def _new_id() -> str:
    return uuid.uuid4().hex


def make_hello(
    client_id: str,
    *,
    security_tier: str = "lowpriv",
    a2l_id: str = "",
    nonce: str = "",
    server_fingerprint: str = "",
) -> Frame:
    return Frame(
        type="hello",
        id=_new_id(),
        meta={
            "client_id": str(client_id or ""),
            "security_tier": str(security_tier or ""),
            "a2l_id": str(a2l_id or ""),
            "nonce": str(nonce or ""),
            "server_fingerprint": str(server_fingerprint or ""),
            "protocol_version": PROTOCOL_VERSION,
        },
    )


def make_hello_ok(
    broker_id: str,
    *,
    security_tier: str = "lowpriv",
    threat_model: str = "T1",
    capabilities: dict[str, Any] | None = None,
    a2l_id: str = "",
    device_fingerprint: str = "",
    nonce: str = "",
    attestation: dict[str, str] | None = None,
) -> Frame:
    meta = {
        "broker_id": str(broker_id or ""),
        "security_tier": str(security_tier or ""),
        "threat_model": str(threat_model or ""),
        "capabilities": dict(capabilities or {}),
        "a2l_id": str(a2l_id or ""),
        "device_fingerprint": str(device_fingerprint or ""),
        "nonce": str(nonce or ""),
        "protocol_version": PROTOCOL_VERSION,
    }
    if attestation:
        meta.update(
            {
                str(key): str(value)
                for key, value in attestation.items()
            }
        )
    return Frame(type="hello_ok", id=_new_id(), meta=meta)


def make_heartbeat() -> Frame:
    return Frame(type="heartbeat", id=_new_id())


def make_request(
    kind: str,
    *,
    op_id: str,
    workspace: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    grant_id: str = "",
    deadline_ms: int | None = None,
    request_id: str | None = None,
) -> Frame:
    return Frame(
        type="request",
        id=request_id or _new_id(),
        op_id=str(op_id or ""),
        kind=str(kind or ""),
        grant_id=str(grant_id or ""),
        workspace=workspace,
        payload=dict(payload or {}),
        deadline_ms=deadline_ms,
    )


def make_response(
    request: Frame,
    *,
    status: str = "ok",
    data: dict[str, Any] | None = None,
    exit_code: int | None = None,
    error_code: str = "",
    error: str = "",
) -> Frame:
    return Frame(
        type="response",
        id=request.id,
        op_id=request.op_id,
        status=status,
        exit_code=exit_code,
        data=dict(data or {}),
        error_code=error_code,
        error=error,
    )


def make_error(
    request: Frame,
    code: str,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> Frame:
    return make_response(
        request,
        status="error",
        data=data,
        error_code=code,
        error=message,
    )


def make_cancel(op_id: str, *, request_id: str | None = None) -> Frame:
    return Frame(
        type="cancel", id=request_id or _new_id(), op_id=str(op_id or "")
    )


def make_grant_request(
    *,
    scope: tuple[str, ...] | list[str],
    verbs: tuple[str, ...] | list[str],
    ttl_s: int,
    reason: str,
    sensitive: bool = False,
    one_shot: bool = False,
    request_id: str | None = None,
) -> Frame:
    return Frame(
        type="grant.request",
        id=request_id or _new_id(),
        payload={
            "scope": list(scope),
            "verbs": list(verbs),
            "ttl_s": int(ttl_s),
            "reason": str(reason or ""),
            "sensitive": bool(sensitive),
            "one_shot": bool(one_shot),
        },
    )


def make_grant_pending(request: Frame) -> Frame:
    return Frame(type="grant.pending", id=request.id)


def make_grant_granted(
    request: Frame,
    *,
    grant_id: str,
    expires_at: str,
    one_shot: bool = False,
) -> Frame:
    return Frame(
        type="grant.granted",
        id=request.id,
        grant_id=str(grant_id or ""),
        payload={
            "expires_at": str(expires_at or ""),
            "one_shot": bool(one_shot),
        },
    )


def make_grant_denied(request: Frame, *, reason: str = "") -> Frame:
    return Frame(
        type="grant.denied",
        id=request.id,
        payload={"reason": str(reason or "")},
    )


def make_grant_revoke(grant_id: str) -> Frame:
    return Frame(type="grant.revoke", grant_id=str(grant_id or ""))


def with_ids(frame: Frame, request: Frame) -> Frame:
    """把 handler 返回的帧绑定到原请求的 id/op_id。"""
    return replace(frame, id=request.id, op_id=request.op_id, v=PROTOCOL_VERSION)
