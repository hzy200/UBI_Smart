"""共享索引缓存的跨进程锁（best-effort）。

用 `O_CREAT|O_EXCL` 创建锁文件，因此不依赖 fcntl/msvcrt 的语义，理论上
也能在 SMB/NFS 上工作（是否真正原子取决于文件服务器）。锁文件带 token；
释放时只删除自己持有的锁，避免 stale takeover 后误删新持有者。

注意：这是**协调**锁，不是安全边界。文件系统不支持 O_EXCL 时退化为
短暂竞争；最坏结果是重复解析/写入，不会损坏内容（内容寻址 + 原子替换）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import TracebackType
from typing import Any


class CacheLockTimeout(TimeoutError):
    pass


class CacheLock:
    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 5.0,
        stale_after: float = 60.0,
    ) -> None:
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self.stale_after = max(1.0, float(stale_after))
        self.token = (
            f"{os.getpid()}:{time.time():.6f}:{os.urandom(4).hex()}"
        )
        self._held = False

    def _stale(self) -> bool:
        try:
            return (
                time.time() - self.path.stat().st_mtime
            ) > self.stale_after
        except OSError:
            return False

    def __enter__(self) -> CacheLock:
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                if self._stale():
                    try:
                        self.path.unlink()
                        continue
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise CacheLockTimeout(
                        f"等待缓存锁超时：{self.path}"
                    ) from None
                time.sleep(0.02)
                continue
            except OSError as exc:
                raise CacheLockTimeout(
                    f"无法创建缓存锁：{self.path}: {exc}"
                ) from exc
            try:
                os.write(
                    fd,
                    json.dumps(
                        {"token": self.token, "pid": os.getpid()}
                    ).encode("utf-8"),
                )
            finally:
                os.close(fd)
            self._held = True
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._held:
            return
        self._held = False
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                isinstance(payload, dict)
                and payload.get("token") == self.token
            ):
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            # 锁文件可能已被 stale takeover；不要误删别人的锁。
            pass
