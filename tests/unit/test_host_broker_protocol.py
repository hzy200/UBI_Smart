"""A2L 协议帧：版本、校验、错误码与幂等字段。"""

from __future__ import annotations

import json

import pytest

from coding_agent.host_broker.protocol import (
    MAX_FRAME_BYTES,
    OP_UNKNOWN,
    PROTOCOL_VERSION,
    PROTOCOL_VERSION_ERROR,
    Frame,
    ProtocolError,
    decode_frame,
    encode_frame,
    make_error,
    make_request,
    make_response,
)


def test_request_roundtrip_keeps_ids_and_payload() -> None:
    frame = make_request(
        "host.read_text",
        op_id="op-1",
        workspace={"workspace_id": "ws-1", "kind": "host", "root_handle": "h-1"},
        payload={"path": "a.py"},
        grant_id="g-1",
        deadline_ms=5000,
    )
    decoded = decode_frame(encode_frame(frame))
    assert decoded == frame
    assert decoded.type == "request"
    assert decoded.op_id == "op-1"
    assert decoded.workspace is not None
    assert decoded.workspace["root_handle"] == "h-1"


def test_response_and_error_helpers_bind_to_request() -> None:
    request = make_request("host.health", op_id="op-1")
    response = make_response(request, data={"ok": True})
    assert response.id == request.id
    assert response.op_id == request.op_id
    assert response.status == "ok"
    error = make_error(request, "DENIED_ZX", "no")
    assert error.status == "error"
    assert error.error_code == "DENIED_ZX"


def test_protocol_version_mismatch_is_rejected() -> None:
    raw = json.dumps(
        {"v": PROTOCOL_VERSION + 1, "type": "heartbeat", "id": "1"}
    )
    with pytest.raises(ProtocolError) as exc:
        decode_frame(raw)
    assert exc.value.code == PROTOCOL_VERSION_ERROR


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ProtocolError) as exc:
        decode_frame(json.dumps({"v": 1, "type": "bogus"}))
    assert exc.value.code == OP_UNKNOWN


def test_request_requires_kind_and_op_id() -> None:
    with pytest.raises(ProtocolError) as exc:
        decode_frame(json.dumps({"v": 1, "type": "request", "id": "1"}))
    assert exc.value.code == "MALFORMED_FRAME"


def test_oversized_and_malformed_frames_are_rejected() -> None:
    with pytest.raises(ProtocolError):
        decode_frame("{" + "x" * (MAX_FRAME_BYTES + 1))
    with pytest.raises(ProtocolError):
        decode_frame("not-json")


def test_frame_rejects_non_object_fields() -> None:
    raw = json.dumps(
        {"v": 1, "type": "request", "id": "1", "op_id": "o", "kind": "host.health", "payload": []}
    )
    with pytest.raises(ProtocolError):
        decode_frame(raw)


def test_frame_serialization_omits_empty_fields() -> None:
    frame = Frame(type="heartbeat", id="h")
    assert frame.to_dict() == {"v": 1, "type": "heartbeat", "id": "h"}
