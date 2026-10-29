"""Linux 本地执行后端。

Phase 0：当 Agent Server 直接运行在 WSL/Linux 内时，不再为每条命令启动
`wsl.exe`，而是直接启动 `bash -l -s`。接口与 `WslSandbox` 保持一致，
便于 `SandboxFs`、工具层和快照层复用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import TypeVar, cast

from coding_agent.config import Settings, get_settings
from coding_agent.platform.concurrency import SyncLeasePool
from coding_agent.sandbox.limits import (
    TIMEOUT_EXIT_CODE,
    ResourceLimits,
    wrap_with_limits,
)
from coding_agent.sandbox.pathguard import normalize_root
from coding_agent.sandbox.wsl_exec import (
    OUTER_TIMEOUT_GRACE,
    ExecResult,
    build_bwrap_script,
)

_T = TypeVar("_T")


class LocalExecError(RuntimeError):
    """本地执行失败。"""


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n...<省略 {len(text) - limit} 字符>...\n{text[-tail:]}"


class LocalLinuxSandbox:
    """在 Linux 内直接执行 bash 的沙箱后端。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        slots: SyncLeasePool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._slots = slots or SyncLeasePool(
            "local",
            int(getattr(self.settings, "sandbox_max_concurrent_commands", 0) or 0),
            reserve=int(
                getattr(self.settings, "concurrency_wsl_reserve", 1) or 0
            ),
        )
        self._probe_cache: dict[str, object] = {}
        self._probe_lock = threading.Lock()
        self._bind_lock = threading.Lock()
        self._extra_binds: set[str] = set()
        self._stats_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_procs: set[subprocess.Popen] = set()
        self._command_count = 0
        self._bwrap_ok: bool | None = None
        self._home = os.path.expanduser("~")

    @property
    def command_count(self) -> int:
        with self._stats_lock:
            return self._command_count

    def _bump_command_count(self) -> None:
        with self._stats_lock:
            self._command_count += 1

    def close(self) -> None:
        return

    def circuit_state(self) -> str:
        return "closed"

    def health(self, *, force: bool = False) -> bool:
        return True

    def cached_probe(self, key: str, producer: Callable[[], _T]) -> _T:
        with self._probe_lock:
            if key not in self._probe_cache:
                self._probe_cache[key] = producer()
            return cast(_T, self._probe_cache[key])

    def forget_probe(self, key: str) -> None:
        with self._probe_lock:
            self._probe_cache.pop(key, None)

    def home(self) -> str:
        return self.cached_probe("home", lambda: self._home)

    def add_extra_bind(self, path: str) -> None:
        try:
            normalized = normalize_root(path)
        except Exception:  # noqa: BLE001 - 非法路径不加入绑定
            return
        with self._bind_lock:
            self._extra_binds.add(normalized)

    def ensure_directory(self, path: str) -> None:
        normalized = normalize_root(path)
        os.makedirs(normalized, exist_ok=True)

    def bwrap_available(self) -> bool:
        if self._bwrap_ok is None:
            self._bwrap_ok = shutil.which("bwrap") is not None
        return self._bwrap_ok

    @property
    def limits(self) -> ResourceLimits:
        settings = self.settings
        return ResourceLimits(
            cpu_seconds=settings.shell_cpu_seconds,
            memory_mb=settings.shell_memory_mb,
            max_file_mb=settings.shell_max_file_mb,
            max_processes=settings.shell_max_processes,
        )

    @property
    def capture_limit(self) -> int:
        return max(0, int(getattr(self.settings, "max_capture_bytes", 0) or 0))

    def _env(self) -> dict[str, str]:
        env = {
            "HOME": self._home,
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "dumb",
        }
        # 与 WSL 后端一样，只放行显式配置的非敏感构建变量。
        for raw in str(self.settings.shell_env_allowlist or "").split(","):
            name = raw.strip()
            if not name or not name.replace("_", "").isalnum():
                continue
            if any(
                token in name.upper()
                for token in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
            ):
                continue
            if name in os.environ:
                env[name] = os.environ[name]
        return env

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        capture_limit: int | None = None,
    ) -> ExecResult:
        wall = timeout if timeout is not None else self.settings.shell_timeout
        limit = 0 if capture_limit is None else max(0, int(capture_limit))
        workdir = cwd or self.settings.wsl_workspace or self.home()

        if self.settings.shell_sandbox == "bwrap":
            if not self.bwrap_available():
                raise LocalExecError(
                    "AGENT_SHELL_SANDBOX=bwrap，但本地 bubblewrap 不可用"
                )
            with self._bind_lock:
                extra_binds = tuple(sorted(self._extra_binds))
            body = build_bwrap_script(
                command,
                workdir=workdir,
                workspace=self.settings.wsl_workspace,
                network=self.settings.shell_network,
                extra_binds=extra_binds,
            )
            popen_cwd = None
        else:
            body = command
            popen_cwd = workdir

        script = wrap_with_limits(
            body, limits=self.limits, wall_seconds=wall
        )
        self._bump_command_count()
        started = time.perf_counter()
        lease_timeout = (wall or 60) + OUTER_TIMEOUT_GRACE
        with self._slots.lease(timeout=lease_timeout):
            try:
                proc = subprocess.Popen(
                    ["/bin/bash", "-l", "-s"],
                    cwd=popen_cwd,
                    env=self._env(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                raise LocalExecError(f"无法启动本地 bash：{exc}") from exc
            timed_out = False
            with self._active_lock:
                self._active_procs.add(proc)
            try:
                try:
                    stdout_b, stderr_b = proc.communicate(
                        script.encode("utf-8"),
                        timeout=(wall or 60) + OUTER_TIMEOUT_GRACE,
                    )
                except subprocess.TimeoutExpired:
                    timed_out = True
                    proc.kill()
                    stdout_b, stderr_b = proc.communicate()
            finally:
                with self._active_lock:
                    self._active_procs.discard(proc)
        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout = _truncate(stdout_b.decode("utf-8", "replace"), limit)
        stderr = _truncate(stderr_b.decode("utf-8", "replace"), limit)
        return ExecResult(
            command=command,
            exit_code=TIMEOUT_EXIT_CODE if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    def cancel_active(self) -> int:
        """终止当前在飞的本机 bash 进程（best-effort）。"""
        killed = 0
        with self._active_lock:
            procs = list(self._active_procs)
            self._active_procs.clear()
        for proc in procs:
            try:
                proc.kill()
                killed += 1
            except OSError:
                continue
        return killed
