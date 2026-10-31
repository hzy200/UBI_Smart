"""受控执行：只允许已注册工具链的 argv，且只在 AppContainer 内运行。

这里校验的是“能不能执行”，真正的隔离由 `AppContainerRunner` 提供。
不接受 shell 字符串，也不把宿主 `PATH` 透给子进程。
"""

from __future__ import annotations

import os
from typing import Any

from ubismart.host_broker.errors import BrokerOpError
from ubismart.host_broker.paths import HostPathError, HostPathPolicy
from ubismart.host_broker.protocol import (
    EXEC_UNAVAILABLE,
    INVALID_REQUEST,
    TIMEOUT,
    TOOLCHAIN_UNAVAILABLE,
)
from ubismart.host_broker.toolchain import ToolchainRef, ToolchainRegistry
from ubismart.host_broker.win_isolation import IsolationError

MAX_ARGV = 128
MAX_ARG_LENGTH = 4096
MAX_TIMEOUT_S = 600.0
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_CAPTURE = 200_000


def _argv(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("argv")
    if not isinstance(raw, list) or not raw:
        raise BrokerOpError(INVALID_REQUEST, "argv 必须是非空数组")
    if len(raw) > MAX_ARGV:
        raise BrokerOpError(INVALID_REQUEST, "argv 参数过多")
    argv: list[str] = []
    for item in raw:
        text = str(item)
        if not text or len(text) > MAX_ARG_LENGTH or "\x00" in text:
            raise BrokerOpError(INVALID_REQUEST, "argv 参数非法")
        argv.append(text)
    return argv


def _env(toolchain: ToolchainRef, writable_dir: str) -> dict[str, str]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    env = {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "TEMP": writable_dir,
        "TMP": writable_dir,
        "PATH": toolchain.root,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1",
    }
    return {key: value for key, value in env.items() if value}


def run_host_exec(
    *,
    runner: Any,
    toolchain: ToolchainRef,
    registry: ToolchainRegistry,
    toolchain_id: str,
    payload: dict[str, Any],
    workspace_root: str,
    output_roots: list[str],
    path_policy: HostPathPolicy,
) -> dict[str, Any]:
    argv = _argv(payload)
    command = argv[0]
    if command not in toolchain.commands:
        raise BrokerOpError(
            TOOLCHAIN_UNAVAILABLE,
            f"工具链 {toolchain_id} 不允许命令 {command!r}",
        )
    executable = registry.resolve(toolchain_id, command)
    if not executable:
        raise BrokerOpError(
            TOOLCHAIN_UNAVAILABLE, f"工具链 {toolchain_id} 未注册命令 {command!r}"
        )

    relative_cwd = str(payload.get("cwd") or ".")
    try:
        cwd = path_policy.resolve_child(workspace_root, relative_cwd)
    except HostPathError as exc:
        raise BrokerOpError(exc.code, exc.message) from exc
    if not os.path.isdir(cwd):
        raise BrokerOpError(INVALID_REQUEST, f"工作目录不存在：{relative_cwd}")
    if not output_roots:
        raise BrokerOpError(
            EXEC_UNAVAILABLE, "工具链未声明 output_roots，拒绝执行"
        )
    for path in output_roots:
        os.makedirs(path, exist_ok=True)

    try:
        timeout = float(payload.get("timeout_s", DEFAULT_TIMEOUT_S))
        capture_limit = int(
            payload.get("capture_limit", DEFAULT_CAPTURE)
        )
    except (TypeError, ValueError) as exc:
        raise BrokerOpError(
            INVALID_REQUEST, "timeout_s/capture_limit 必须是数字"
        ) from exc
    timeout = max(1.0, min(timeout, MAX_TIMEOUT_S))
    capture_limit = max(1024, min(capture_limit, 2_000_000))
    env = _env(toolchain, output_roots[0])

    try:
        result = runner.exec(
            [executable, *argv[1:]],
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_limit=capture_limit,
        )
    except IsolationError as exc:
        raise BrokerOpError(EXEC_UNAVAILABLE, str(exc)) from exc

    result = dict(result)
    result["toolchain"] = toolchain_id
    result["command"] = command
    if result.get("timed_out"):
        result["error_code"] = TIMEOUT
    return result
