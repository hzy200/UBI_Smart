"""WSL server 崩溃看门狗（Phase 5）。

窗口打开期间轮询子进程；意外退出时最多重启 3 次，成功则回调新 server
（launcher 用新 token/端口重载页面）。用户关窗时通过 stop_event 停止，
不会再拉起新进程。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

BACKOFF_S = (1.0, 3.0, 7.0)


@dataclass
class WatchTarget:
    """一个需要守护的进程；restart 返回新句柄并复用原端口/token。"""

    name: str
    server: Any
    restart: Callable[[], Any]


def supervise(
    server: Any,
    restart: Callable[[], Any],
    on_restart: Callable[[Any], None],
    stop_event: threading.Event,
    *,
    poll_interval: float = 2.0,
    backoff: tuple[float, ...] = BACKOFF_S,
    sleep: Callable[[float], Any] = time.sleep,
    attempts: int = 3,
) -> str:
    """阻塞式看门狗；返回 `stopped` 或 `failed`，便于启动器记录日志。"""
    while not stop_event.is_set():
        proc = getattr(server, "proc", None)
        if proc is None or proc.poll() is None:
            sleep(poll_interval)
            continue
        for delay in backoff[:attempts]:
            if stop_event.wait(delay):
                return "stopped"
            try:
                fresh = restart()
            except Exception:  # noqa: BLE001 - 每次失败都按退避继续
                continue
            try:
                on_restart(fresh)
            except Exception:  # noqa: BLE001 - 页面重载失败也要继续守护
                pass
            server = fresh
            break
        else:
            return "failed"
    return "stopped"


def supervise_many(
    targets: list[WatchTarget],
    stop_event: threading.Event,
    *,
    poll_interval: float = 2.0,
    backoff: tuple[float, ...] = BACKOFF_S,
    sleep: Callable[[float], Any] = time.sleep,
    attempts: int = 3,
) -> str:
    """同时守护 A2/A3；任一进程重启失败返回 `failed:<name>`。"""
    while not stop_event.is_set():
        for target in targets:
            proc = getattr(target.server, "proc", None)
            if proc is None or proc.poll() is None:
                continue
            for delay in backoff[:attempts]:
                if stop_event.wait(delay):
                    return "stopped"
                try:
                    target.server = target.restart()
                except Exception:  # noqa: BLE001 - 按退避继续尝试
                    continue
                break
            else:
                return f"failed:{target.name}"
        sleep(poll_interval)
    return "stopped"
