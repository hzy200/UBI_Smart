"""兼容导入：实现已移到 `coding_agent.platform.filelock`。"""

from __future__ import annotations

from coding_agent.platform.filelock import (
    FileLock,
    NullLock,
    file_lock,
    lock_path_for,
)

__all__ = ["FileLock", "NullLock", "file_lock", "lock_path_for"]
