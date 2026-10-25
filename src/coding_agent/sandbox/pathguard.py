"""路径守卫与 Windows ↔ WSL 路径转换（兼容层）。

实现已移到中立的 `coding_agent.paths`，这样控制面（A2）也能复用词法校验，
而不必依赖执行面。本模块保留原有导入路径与 `SandboxPathError` 名字。

符号链接逃逸需要文件系统信息，由 `sandbox/fs.py` 在沙箱内做 `realpath`
二次校验补齐。
"""

from __future__ import annotations

from coding_agent.paths import (
    PathGuardError,
    SandboxPathError,
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
    "SandboxPathError",
    "ensure_inside",
    "is_within",
    "looks_like_windows_path",
    "normalize_root",
    "normalize_wsl",
    "strip_wsl_scheme",
    "win_to_wsl",
    "wsl_to_win",
]
