"""Web 前后端版本契约。

`CAPABILITIES_VERSION` 在 `/api/capabilities` 的字段或语义发生不兼容变化时
递增；前端据此决定是否重新拉取能力。
"""

from __future__ import annotations

import re

# 前端请求 /api/v1/*；A2 同时保留 /api/* 作为兼容窗口（带 Deprecation）。
WEB_API_VERSION = "v1"
MIN_API_VERSION = "v0"
MIN_FRONTEND_VERSION = "0.1.0"
CAPABILITIES_VERSION = 1

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_semver(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.match(str(value or "").strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def frontend_is_too_old(value: str, minimum: str) -> bool | None:
    """None 表示版本号非法；True/False 表示比较结果。"""
    current = parse_semver(value)
    required = parse_semver(minimum)
    if current is None or required is None:
        return None
    return current < required
