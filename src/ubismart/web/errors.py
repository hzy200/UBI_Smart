"""统一错误协议（application/problem+json）与 request id。"""

from __future__ import annotations

import re
import uuid
from http import HTTPStatus
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from coding_agent.platform.request_context import REQUEST_ID_HEADER, current_request_id

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

_STATUS_CODES: dict[int, str] = {
    400: "request.invalid",
    401: "auth.required",
    403: "auth.forbidden",
    404: "request.not_found",
    405: "request.method_not_allowed",
    408: "request.timeout",
    409: "request.conflict",
    411: "request.length_required",
    413: "request.too_large",
    415: "request.unsupported_media",
    422: "request.invalid",
    425: "request.too_early",
    426: "version.frontend_too_old",
    429: "rate.limited",
    500: "server.error",
    502: "gateway.error",
    503: "server.unavailable",
    504: "gateway.timeout",
}

_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def ensure_request_id(value: str | None) -> str:
    text = str(value or "").strip()
    if text and _REQUEST_ID_RE.match(text):
        return text
    return uuid.uuid4().hex


def request_id_from_scope(scope: dict[str, Any]) -> str:
    state = scope.get("state") or {}
    return str(state.get("request_id") or current_request_id() or "")


def status_error_code(status_code: int) -> str:
    return _STATUS_CODES.get(int(status_code), "server.error")


def is_retryable_status(status_code: int) -> bool:
    return int(status_code) in _RETRYABLE


def problem_response(
    *,
    status_code: int,
    code: str | None = None,
    detail: Any = "",
    details: dict[str, Any] | None = None,
    retryable: bool | None = None,
    headers: dict[str, str] | None = None,
    instance: str = "",
    request_id: str = "",
) -> JSONResponse:
    resolved_code = code or status_error_code(status_code)
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:
        title = "Error"
    rid = request_id or current_request_id() or ensure_request_id(None)
    payload: dict[str, Any] = {
        "type": f"urn:codingagent:error:{resolved_code}",
        "title": title,
        "status": int(status_code),
        "code": resolved_code,
        "detail": detail if isinstance(detail, str) else jsonable_encoder(detail),
        "instance": instance,
        "request_id": rid,
        "retryable": (
            is_retryable_status(status_code)
            if retryable is None
            else bool(retryable)
        ),
    }
    if details:
        payload["details"] = jsonable_encoder(details)
    response_headers = dict(headers or {})
    response_headers.setdefault(REQUEST_ID_HEADER, rid)
    return JSONResponse(
        payload,
        status_code=int(status_code),
        headers=response_headers,
        media_type="application/problem+json",
    )
