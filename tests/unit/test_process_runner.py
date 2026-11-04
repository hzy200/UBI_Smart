"""进程兜底：spawn 执行、超时杀进程、异常回传。"""

from __future__ import annotations

import os
import time

import pytest

from ubismart.platform.process_runner import (
    ProcessError,
    ProcessTimeout,
    run_in_process,
)


def _echo(value: str) -> str:
    return f"{value}:{os.getpid()}"


def _sleepy(seconds: float) -> str:
    time.sleep(seconds)
    return "done"


def _boom() -> str:
    raise ValueError("child failed")


def test_run_in_process_returns_child_result() -> None:
    result = run_in_process(_echo, ("x",), timeout_s=10)

    assert result.startswith("x:")
    assert result.split(":")[1] != str(os.getpid())


def test_run_in_process_kills_a_hung_child() -> None:
    with pytest.raises(ProcessTimeout):
        run_in_process(_sleepy, (5.0,), timeout_s=0.2)


def test_child_exception_is_returned_as_process_error() -> None:
    with pytest.raises(ProcessError, match="child failed"):
        run_in_process(_boom, timeout_s=10)
