"""文件工具的纯辅助函数（无 I/O）。"""

from __future__ import annotations


def _changed_since_read(cached: bytes | None, current: str | None) -> bool:
    """模型读到写之间文件是否被外部改过。

    只用**模型自己读过的缓存**做比较；没有读取记录就不猜。
    这不是安全边界，而是防止 `file_write` 静默覆盖编辑器/构建工具的改动。
    """
    if current is None or cached is None:
        return False
    return current.encode("utf-8") != cached


def _relpath(path: str, root: str) -> str:
    """展示用的工作区相对路径。"""
    prefix = root.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path
