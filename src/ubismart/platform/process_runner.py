"""L-C 进程兜底：不可取消/CPU 密集的同步工作放进可丢弃的进程。

线程 kill 不掉；一旦底层库挂起，线程会永久占用。这里用 `spawn` 子进程
执行一个**可 pickle 的顶层函数**，父进程按 timeout 轮询，超时直接
terminate/kill 并回收。容器级配额不在这一层；它只保证“卡死能收”。

适用：文档解析、向量/CPU 密集计算、已知不可取消的同步 SDK。
不适用：需要沙箱/长连接的调用；那些仍走 `wsl.exe` 子进程或专用后端。
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import traceback
from collections.abc import Callable
from typing import Any

from ubismart.platform.observability import inc, inc_gauge


class ProcessError(RuntimeError):
    """子进程执行失败（非超时）。"""


class ProcessTimeout(ProcessError):
    """子进程超时，已强制回收。"""


class ProcessBusy(ProcessError):
    """进程并发已满，快速失败而不是无限排队。"""


_MAX_CONCURRENT = 2
_SEMAPHORE = threading.BoundedSemaphore(_MAX_CONCURRENT)
_ACTIVE = 0
_ACTIVE_LOCK = threading.Lock()


def configure_process_runner(*, max_concurrent: int = 2) -> None:
    """按部署调整并发；只在启动阶段调用。"""
    global _MAX_CONCURRENT, _SEMAPHORE
    _MAX_CONCURRENT = max(1, int(max_concurrent or 1))
    _SEMAPHORE = threading.BoundedSemaphore(_MAX_CONCURRENT)


def _child_entry(
    conn: Any,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    try:
        result = func(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - 子进程边界必须回传失败
        try:
            conn.send(
                (
                    "error",
                    f"{type(exc).__name__}: {exc}",
                    traceback.format_exc()[-4_000:],
                )
            )
        finally:
            conn.close()
        return
    try:
        conn.send(("ok", result))
    finally:
        conn.close()


def run_in_process(
    func: Callable[..., Any],
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    *,
    timeout_s: float = 0.0,
    name: str = "",
) -> Any:
    """在 spawn 子进程中执行 `func`；超时直接杀进程。

    `func` 必须是可 pickle 的顶层函数，参数/返回值也必须可 pickle。
    `timeout_s <= 0` 表示不设超时（仍建议设置）。
    """
    global _ACTIVE
    acquired = _SEMAPHORE.acquire(timeout=max(0.1, timeout_s or 60.0))
    if not acquired:
        inc("agent_process_runs_total", {"outcome": "busy"})
        raise ProcessBusy("进程隔离并发已满")
    with _ACTIVE_LOCK:
        _ACTIVE += 1
    inc_gauge("agent_process_active", 1)
    context = multiprocessing.get_context("spawn")
    parent_conn, child_conn = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_entry,
        args=(child_conn, func, args, dict(kwargs or {})),
        name=name or getattr(func, "__name__", "process-task"),
        daemon=True,
    )
    # 子进程不需要覆盖率插桩。pytest-cov 会把 COV_CORE_* 写进环境，
    # spawn 子进程再各自写同一个 data file 会互相锁库，表现为文档解析
    # 进程随机失败。启动期间临时摘掉这些变量即可（父进程随后恢复）。
    coverage_env = {
        key: os.environ.pop(key)
        for key in (
            "COV_CORE_SOURCE",
            "COV_CORE_CONFIG",
            "COV_CORE_DATAFILE",
            "COV_CORE_SESSION",
            "COVERAGE_PROCESS_START",
        )
        if key in os.environ
    }
    try:
        process.start()
        # 父进程不持有子端；否则 poll 永远等不到 EOF。
        child_conn.close()
    finally:
        os.environ.update(coverage_env)
    try:
        if timeout_s > 0:
            ready = parent_conn.poll(timeout_s)
        else:
            ready = parent_conn.poll()
        if not ready:
            process.terminate()
            process.join(timeout=2.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=2.0)
            inc("agent_process_runs_total", {"outcome": "timeout"})
            raise ProcessTimeout(
                f"进程任务超时（>{timeout_s:g}s），已回收：{name or 'task'}"
            )
        try:
            status, payload, *extra = parent_conn.recv()
        except EOFError as exc:
            inc("agent_process_runs_total", {"outcome": "error"})
            raise ProcessError(
                f"进程任务未返回结果：{name or 'task'}"
            ) from exc
        if status == "error":
            inc("agent_process_runs_total", {"outcome": "error"})
            detail = payload
            tb = extra[0] if extra else ""
            raise ProcessError(f"{detail}\n{tb}".strip())
        inc("agent_process_runs_total", {"outcome": "ok"})
        return payload
    finally:
        parent_conn.close()
        process.join(timeout=2.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
        with _ACTIVE_LOCK:
            _ACTIVE = max(0, _ACTIVE - 1)
        inc_gauge("agent_process_active", -1)
        _SEMAPHORE.release()
