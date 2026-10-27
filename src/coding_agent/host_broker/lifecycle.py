"""A2L 客户端生命周期：随客户端启停、单实例、暂停/离线。

不注册系统服务，也不做操作系统级自启；`start()` / `stop()` 由客户端
监督进程调用。单实例用 pid 锁文件实现，锁文件只防同一用户重复启动。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STATE_STOPPED = "stopped"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_OFFLINE = "offline"


def default_lock_path() -> Path:
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        ) / "CodingAgent"
    else:
        base = Path.home() / ".coding-agent"
    return base / "a2l.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    state: str
    serving: bool
    client_id: str
    pid: int
    lock_path: str


class BrokerLifecycle:
    def __init__(
        self,
        lock_path: str | Path | None = None,
        *,
        client_id: str = "",
    ) -> None:
        self.lock_path = Path(lock_path or default_lock_path())
        self.client_id = str(client_id or "")
        self.state = STATE_STOPPED
        self._owned = False

    def start(self) -> bool:
        if self.state != STATE_STOPPED:
            return False
        if not self._acquire():
            return False
        self.state = STATE_RUNNING
        return True

    def stop(self) -> None:
        self._release()
        self.state = STATE_STOPPED

    def pause(self) -> None:
        if self.state in {STATE_RUNNING, STATE_OFFLINE}:
            self.state = STATE_PAUSED

    def resume(self) -> None:
        if self.state == STATE_PAUSED:
            self.state = STATE_RUNNING

    def mark_offline(self) -> None:
        if self.state == STATE_RUNNING:
            self.state = STATE_OFFLINE

    def mark_online(self) -> None:
        if self.state == STATE_OFFLINE:
            self.state = STATE_RUNNING

    def can_serve(self) -> bool:
        return self.state == STATE_RUNNING

    def snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            state=self.state,
            serving=self.can_serve(),
            client_id=self.client_id,
            pid=os.getpid(),
            lock_path=str(self.lock_path),
        )

    def _acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "client_id": self.client_id,
                "started_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        for _ in range(2):
            try:
                fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                if self._owner_alive():
                    return False
                # 遗留锁：删除后重试一次。
                try:
                    self.lock_path.unlink()
                except OSError:
                    return False
                continue
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
            except OSError:
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
                return False
            self._owned = True
            return True
        return False

    def _owner_alive(self) -> bool:
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        return _pid_alive(pid)

    def _release(self) -> None:
        if not self._owned:
            return
        try:
            self.lock_path.unlink()
        except OSError:
            pass
        self._owned = False
