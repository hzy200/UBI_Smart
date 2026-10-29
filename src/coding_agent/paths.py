"""兼容导入：实现已移到 `coding_agent.platform.paths`。"""

from __future__ import annotations

from coding_agent.platform.paths import (
    PathGuardError,
    ensure_inside,
    is_within,
    looks_like_windows_path,
    normalize_root,
    normalize_wsl,
    strip_wsl_scheme,
    win_to_wsl,
    wsl_to_win,
)

__all__ = [
    "PathGuardError",
    "ensure_inside",
    "is_within",
    "looks_like_windows_path",
    "normalize_root",
    "normalize_wsl",
    "strip_wsl_scheme",
    "win_to_wsl",
    "wsl_to_win",
]
