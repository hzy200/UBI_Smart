"""兼容导入：实现已移到 `coding_agent.platform.observability`。"""

from __future__ import annotations

from coding_agent.platform.observability import (
    JsonFormatter,
    _parse_otel_headers,
    configure_logging,
    configure_tracing,
    inc,
    inc_gauge,
    note_silent_failure,
    render,
    set_gauge,
    snapshot,
    tracer,
)

__all__ = [
    "JsonFormatter",
    "_parse_otel_headers",
    "configure_logging",
    "configure_tracing",
    "inc",
    "inc_gauge",
    "note_silent_failure",
    "render",
    "set_gauge",
    "snapshot",
    "tracer",
]
