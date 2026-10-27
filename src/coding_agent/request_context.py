"""请求级上下文（当前只放 request id）。

放在顶层而不是 web/，是因为 A2 的控制面客户端（control/）也需要把
request id 传给 A3，不能反向依赖 web 层。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

REQUEST_ID_HEADER = "X-Request-ID"

_request_id: ContextVar[str] = ContextVar("coding_agent_request_id", default="")


def current_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str) -> Token[str]:
    return _request_id.set(value)


def reset_request_id(token: Token[str]) -> None:
    _request_id.reset(token)
