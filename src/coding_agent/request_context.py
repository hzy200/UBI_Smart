"""兼容导入：实现已移到 `coding_agent.platform.request_context`。"""

from __future__ import annotations

from coding_agent.platform.request_context import (
    REQUEST_ID_HEADER,
    current_request_id,
    reset_request_id,
    set_request_id,
)

__all__ = [
    "REQUEST_ID_HEADER",
    "current_request_id",
    "reset_request_id",
    "set_request_id",
]
