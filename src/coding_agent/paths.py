"""纯路径工具（中立模块）。

放在顶层而不是 `sandbox/`，因为控制面（A2）也需要词法路径校验，
但不能依赖执行面。这里**不访问文件系统**，只做字符串归一化与越界判断；
符号链接逃逸仍由 A3/A4 在沙箱内 `realpath` 补齐。

`sandbox/pathguard.py` 保留为兼容层，既有导入不需要改动。
"""

from __future__ import annotations

import posixpath
import re

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


class PathGuardError(ValueError):
    """路径越界或无法安全解析。"""


# 兼容旧名字：异常类型保持同一个对象，except 子句不会失效。
SandboxPathError = PathGuardError


def looks_like_windows_path(path: str) -> bool:
    """`D:\\proj`、`C:/tmp` 这类带盘符的路径。"""
    return bool(_WIN_DRIVE_RE.match(path))


def strip_wsl_scheme(path: str) -> str:
    """`wsl:/home/u/proj` → `/home/u/proj`。

    Git Bash / MSYS 会把以 `/` 开头的参数自动转换成 Windows 路径
    （`/home/x` → `D:\\Git\\home\\x`）。加 `wsl:` 前缀可以让参数不以 `/`
    开头，从而绕过这层转换。
    """
    text = str(path)
    if text.startswith("wsl://"):
        rest = text[len("wsl://") :]
    elif text.startswith("wsl:"):
        rest = text[len("wsl:") :]
    else:
        return text
    return rest if rest.startswith("/") else f"/{rest}"


def win_to_wsl(path: str) -> str:
    """`D:\\proj\\a` → `/mnt/d/proj/a`；已是 POSIX 路径则原样返回。"""
    if not path:
        raise PathGuardError("路径为空")
    if path.startswith("/"):
        return path
    match = _WIN_DRIVE_RE.match(path)
    if not match:
        raise PathGuardError(f"无法识别的 Windows 路径：{path}")
    drive, rest = match.groups()
    return f"/mnt/{drive.lower()}/{rest.replace(chr(92), '/')}"


def wsl_to_win(path: str) -> str:
    """`/mnt/d/proj/a` → `D:\\proj\\a`；非 /mnt 挂载则原样返回。"""
    match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path)
    if not match:
        return path
    drive, rest = match.groups()
    return f"{drive.upper()}:\\{rest.replace('/', chr(92))}"


def normalize_wsl(path: str, cwd: str = "/") -> str:
    """返回归一化的绝对 POSIX 路径。

    容忍 Windows 形式的输入（`D:\\proj` → `/mnt/d/proj`），因为配置和模型
    都可能给出盘符路径；相对路径按 cwd 展开。`wsl:/home/...` 用于绕过
    Git Bash / MSYS 的自动路径转换。
    """
    path = strip_wsl_scheme(path)
    cwd = strip_wsl_scheme(cwd)
    if "\x00" in path:
        raise PathGuardError("路径包含空字节")
    # 不做 ~ 展开：要么是模型笔误，要么是想逃出工作区，两者都应显式失败
    if path == "~" or path.startswith("~/"):
        raise PathGuardError(
            f"不支持 ~ 展开，请给出工作区内的绝对路径：{path}"
        )
    path = _to_posix(path)
    cwd = _to_posix(cwd)
    candidate = path if path.startswith("/") else posixpath.join(cwd, path)
    return posixpath.normpath(candidate)


def _to_posix(path: str) -> str:
    return win_to_wsl(path) if looks_like_windows_path(path) else path


def normalize_root(root: str) -> str:
    """归一化工作区根目录；必须是绝对路径。"""
    root = strip_wsl_scheme(root)
    if not root.startswith("/") and not looks_like_windows_path(root):
        raise PathGuardError(f"工作区必须是绝对路径：{root}")
    return normalize_wsl(root)


def is_within(path: str, root: str) -> bool:
    """纯词法判断，入参需已是 POSIX 绝对路径。"""
    normalized = posixpath.normpath(path)
    root_norm = posixpath.normpath(root).rstrip("/") or "/"
    if root_norm == "/":
        return normalized.startswith("/")
    return normalized == root_norm or normalized.startswith(root_norm + "/")


def ensure_inside(path: str, root: str, cwd: str = "/") -> str:
    """归一化并确认路径位于 root 之内，返回可直接使用的 POSIX 路径。

    两侧都做归一化 —— 否则工作区根写成 Windows 路径时，连它自己都会被判越界。
    """
    normalized = normalize_wsl(path, cwd)
    root_norm = normalize_root(root)
    if not is_within(normalized, root_norm):
        raise PathGuardError(
            f"路径越出工作区：{normalized} 不在 {root_norm} 内"
        )
    return normalized
