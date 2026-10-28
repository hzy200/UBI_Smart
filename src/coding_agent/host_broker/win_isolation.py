# ruff: noqa: E501 - 内嵌 PowerShell 脚本按可读性保留长行
"""Windows 只读隔离：Job Object、低完整性、AppContainer 读代理。

P0 目标不是宣称 T2：它只把 A2L 的只读面收紧到 OS 层可验证的边界：
- Job Object：限制子进程/内存/CPU，关闭句柄即杀进程树；
- 低完整性：降低写权限（显式开启）；
- AppContainer：在独立容器内执行只读 helper，未授权路径由 OS 拒绝。

非 Windows 平台调用会抛 IsolationError；测试用 skipif 跳过。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.host_broker.win_ctypes import (
    IS_WINDOWS,
    IsolationError,
    assign_job,
    close_job,
    create_appcontainer_profile,
    create_job,
    delete_appcontainer_profile,
    low_integrity_supported,
    set_low_integrity,
    spawn_in_appcontainer,
)


@dataclass(frozen=True, slots=True)
class JobLimits:
    max_processes: int = 32
    job_memory_mb: int = 2048
    process_memory_mb: int = 1024
    ui_restrictions: bool = True


@dataclass(frozen=True, slots=True)
class IsolationReport:
    job_object: bool = False
    low_integrity: bool = False
    appcontainer: bool = False
    security_tier: str = "lowpriv"
    threat_model: str = "T1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_object": self.job_object,
            "low_integrity": self.low_integrity,
            "appcontainer": self.appcontainer,
            "security_tier": self.security_tier,
            "threat_model": self.threat_model,
        }


class JobObject:
    """持有 Job 句柄；句柄关闭时根据标志杀死进程树。"""

    def __init__(self, limits: JobLimits | None = None) -> None:
        self.limits = limits or JobLimits()
        self.handle: int | None = None
        self._self_assigned = False
        self._apply()

    def _apply(self) -> None:
        self.handle = create_job(
            max_processes=self.limits.max_processes,
            process_memory_mb=self.limits.process_memory_mb,
            job_memory_mb=self.limits.job_memory_mb,
            ui_restrictions=self.limits.ui_restrictions,
        )

    def assign(self, process_handle: int | None = None) -> None:
        self._self_assigned = process_handle is None
        if self.handle is None:
            raise IsolationError("Job 句柄已关闭")
        assign_job(self.handle, process_handle)

    def close(self, *, terminate: bool = False) -> None:
        if not self.handle:
            return
        close_job(
            self.handle,
            terminate=terminate,
            self_assigned=self._self_assigned,
        )
        self.handle = None


class AppContainerRunner:
    """在 AppContainer 内执行只读 helper；未授权路径由 OS 拒绝。"""

    def __init__(
        self,
        workspace_roots: list[str] | tuple[str, ...],
        *,
        exec_roots: list[str] | tuple[str, ...] = (),
        write_roots: list[str] | tuple[str, ...] = (),
        profile_name: str = "",
        out_dir: str | Path | None = None,
    ) -> None:
        if not IS_WINDOWS:
            raise IsolationError("AppContainer 仅支持 Windows")
        self.profile_name = profile_name or (
            "CodingAgentRead_" + uuid.uuid4().hex[:12]
        )
        self.powershell_exe = str(
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not Path(self.powershell_exe).is_file():
            raise IsolationError("找不到 Windows PowerShell")
        self._roots = [str(Path(root).resolve()) for root in workspace_roots]
        self._exec_roots = [
            str(Path(root).resolve()) for root in exec_roots
        ]
        self._write_roots = [
            str(Path(root).resolve()) for root in write_roots
        ]
        self._out_dir = Path(
            out_dir
            or Path(
                os.environ.get("LOCALAPPDATA")
                or tempfile.gettempdir()
            )
            / "CodingAgent"
            / "appcontainer-out"
        ) / self.profile_name
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._sid = 0
        self._sid_str = ""
        self._granted: list[tuple[str, bool]] = []
        self._create_profile()
        try:
            for root in [*self._roots, *self._exec_roots]:
                self._grant_tree(root, read=True)
            for root in self._write_roots:
                self._grant_tree(root, read=True)
                self._grant_tree(root, read=False)
            self._grant_tree(self._out_dir, read=False)
        except Exception:
            self.close()
            raise

    @property
    def out_dir(self) -> str:
        return str(self._out_dir)

    def _create_profile(self) -> None:
        self._sid, self._sid_str = create_appcontainer_profile(
            self.profile_name
        )

    def _icacls(self, args: list[str]) -> None:
        result = subprocess.run(
            ["icacls", *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
        if result.returncode not in (0,):
            raise IsolationError(
                f"icacls 失败：{result.returncode} {result.stdout} {result.stderr}"
            )

    def _grant_tree(self, path: str, *, read: bool) -> None:
        target = Path(path)
        if not target.exists():
            return
        perm = "(OI)(CI)(RX)" if read else "(OI)(CI)(M)"
        self._icacls([str(target), "/grant", f"*{self._sid_str}:{perm}", "/T", "/C", "/Q"])
        self._granted.append((str(target), read))

    def _revoke_tree(self, path: str) -> None:
        try:
            subprocess.run(
                [
                    "icacls",
                    path,
                    "/remove:g",
                    f"*{self._sid_str}",
                    "/T",
                    "/C",
                    "/Q",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
            )
        except Exception:
            pass

    def call(
        self,
        op: str,
        args: dict[str, Any],
        *,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        result_path = self._out_dir / f"{uuid.uuid4().hex}.json"
        payload = {"op": op, **args}
        if op == "read":
            payload.setdefault("max_bytes", 262144)
        if op in {"list", "find"}:
            payload.setdefault("limit", 200)
        args_b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        out_b64 = base64.b64encode(
            str(result_path).encode("utf-8")
        ).decode("ascii")
        script = f"""$ErrorActionPreference='Stop'
$a=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{args_b64}')) | ConvertFrom-Json
$out=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{out_b64}'))
function Emit($o) {{ [IO.File]::WriteAllText($out, ($o | ConvertTo-Json -Compress -Depth 6), (New-Object Text.UTF8Encoding($false))) }}
try {{
  switch ($a.op) {{
    'read' {{
      $bytes=[IO.File]::ReadAllBytes($a.path)
      if ($bytes.Length -gt [int]$a.max_bytes) {{ Emit @{{ok=$false;code='FILE_TOO_LARGE';error='file too large'}} }}
      elseif ($bytes -contains 0) {{ Emit @{{ok=$false;code='INVALID_REQUEST';error='binary'}} }}
      else {{ Emit @{{ok=$true;text=[Text.Encoding]::UTF8.GetString($bytes)}} }}
    }}
    'list' {{
      $entries=@(Get-ChildItem -LiteralPath $a.path | Select-Object -First ([int]$a.limit) | ForEach-Object {{
        $kind=if ($_.PSIsContainer) {{'d'}} elseif ($_.LinkType) {{'l'}} else {{'f'}}
        @{{name=$_.Name;kind=$kind}}
      }})
      Emit @{{ok=$true;entries=$entries}}
    }}
    'stat' {{
      try {{
        $item=Get-Item -LiteralPath $a.path -ErrorAction Stop
        $epoch=[datetime]'1970-01-01T00:00:00Z'
        $size=if ($item.PSIsContainer) {{0}} else {{[int64]$item.Length}}
        Emit @{{ok=$true;exists=$true;is_file=(-not $item.PSIsContainer);is_dir=$item.PSIsContainer;size=$size;mtime=[double](($item.LastWriteTimeUtc-$epoch).TotalSeconds)}}
      }} catch [System.IO.FileNotFoundException] {{ Emit @{{ok=$true;exists=$false}} }}
    }}
    'find' {{
      $files=@(Get-ChildItem -LiteralPath $a.path -Recurse -File -ErrorAction SilentlyContinue | Where-Object {{
        $rel=$_.FullName.Substring($a.path.Length).TrimStart('\\') -replace '\\\\','/'
        ($_.Name -like $a.pattern) -or ($rel -like $a.pattern)
      }} | Select-Object -First ([int]$a.limit) | ForEach-Object {{
        $_.FullName.Substring($a.path.Length).TrimStart('\\') -replace '\\\\','/'
      }})
      Emit @{{ok=$true;files=$files}}
    }}
    default {{ throw 'OP_UNKNOWN' }}
  }}
}} catch {{
  $code='INVALID_REQUEST'
  $inner=$_.Exception.InnerException
  if ($_.Exception -is [System.UnauthorizedAccessException] -or $inner -is [System.UnauthorizedAccessException]) {{ $code='ACCESS_DENIED' }}
  elseif ($_.Exception -is [System.IO.FileNotFoundException] -or $inner -is [System.IO.FileNotFoundException]) {{ $code='FILE_NOT_FOUND' }}
  Emit @{{ok=$false;code=$code;error=$_.Exception.Message}}
}}
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        command_line = subprocess.list2cmdline(
            [
                self.powershell_exe,
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ]
        )
        self._spawn(command_line, timeout=timeout)
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
            for key in ("entries", "files"):
                if isinstance(data.get(key), dict):
                    data[key] = [data[key]]
            return data
        finally:
            try:
                result_path.unlink()
            except OSError:
                pass

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: float,
        env: dict[str, str],
        capture_limit: int = 200_000,
    ) -> dict[str, Any]:
        """在 AppContainer 内执行已注册命令，返回 stdout/stderr/exit_code。"""
        if not argv:
            raise IsolationError("argv 不能为空")
        result_path = self._out_dir / f"{uuid.uuid4().hex}.json"
        payload = {
            "exe": argv[0],
            "args": subprocess.list2cmdline(argv),
            "cwd": cwd,
            "env": {str(k): str(v) for k, v in env.items()},
            "timeout_ms": int(max(1.0, timeout) * 1000),
        }
        args_b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        out_b64 = base64.b64encode(
            str(result_path).encode("utf-8")
        ).decode("ascii")
        script = f"""$ErrorActionPreference='Stop'
$a=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{args_b64}')) | ConvertFrom-Json
$out=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{out_b64}'))
function Emit($o) {{ [IO.File]::WriteAllText($out, ($o | ConvertTo-Json -Compress -Depth 6), (New-Object Text.UTF8Encoding($false))) }}
try {{
  $psi=New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName=$a.exe
  $psi.Arguments=$a.args
  $psi.WorkingDirectory=$a.cwd
  $psi.UseShellExecute=$false
  $psi.RedirectStandardOutput=$true
  $psi.RedirectStandardError=$true
  $psi.CreateNoWindow=$true
  $psi.EnvironmentVariables.Clear()
  foreach ($name in $a.env.PSObject.Properties.Name) {{
    $psi.EnvironmentVariables[$name]=[string]$a.env.$name
  }}
  $started=Get-Date
  $p=[System.Diagnostics.Process]::Start($psi)
  $outTask=$p.StandardOutput.ReadToEndAsync()
  $errTask=$p.StandardError.ReadToEndAsync()
  $timedOut=-not $p.WaitForExit([int]$a.timeout_ms)
  if ($timedOut) {{ try {{ $p.Kill() }} catch {{}} }}
  $stdout=$outTask.Result
  $stderr=$errTask.Result
  $code=if ($timedOut) {{ -1 }} else {{ $p.ExitCode }}
  $ms=[int]((Get-Date)-$started).TotalMilliseconds
  Emit @{{ok=$true;exit_code=$code;stdout=$stdout;stderr=$stderr;timed_out=$timedOut;duration_ms=$ms}}
}} catch {{
  Emit @{{ok=$false;error=$_.Exception.Message}}
}}
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        command_line = subprocess.list2cmdline(
            [
                self.powershell_exe,
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ]
        )
        self._spawn(command_line, timeout=max(30.0, timeout + 20.0))
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IsolationError(f"AppContainer exec 结果不可读：{exc}") from exc
        finally:
            try:
                result_path.unlink()
            except OSError:
                pass
        if not data.get("ok"):
            raise IsolationError(
                f"AppContainer exec 失败：{data.get('error') or 'unknown'}"
            )
        limit = max(0, int(capture_limit))
        return {
            "exit_code": int(data.get("exit_code", -1)),
            "stdout": str(data.get("stdout") or "")[:limit],
            "stderr": str(data.get("stderr") or "")[:limit],
            "stdout_truncated": len(str(data.get("stdout") or "")) > limit,
            "stderr_truncated": len(str(data.get("stderr") or "")) > limit,
            "timed_out": bool(data.get("timed_out")),
            "duration_ms": int(data.get("duration_ms", 0)),
        }

    def _spawn(self, command_line: str, *, timeout: float) -> None:
        spawn_in_appcontainer(
            exe=self.powershell_exe,
            command_line=command_line,
            sid=self._sid,
            timeout=timeout,
        )

    def close(self) -> None:
        for path, _ in reversed(self._granted):
            self._revoke_tree(path)
        self._granted.clear()
        if self._sid_str:
            try:
                delete_appcontainer_profile(self.profile_name)
            except Exception:  # noqa: BLE001 - 删除 profile 失败不阻断关闭
                pass
            self._sid_str = ""
        try:
            self._out_dir.rmdir()
        except OSError:
            pass


__all__ = [
    "IS_WINDOWS",
    "AppContainerRunner",
    "IsolationError",
    "IsolationReport",
    "JobLimits",
    "JobObject",
    "low_integrity_supported",
    "set_low_integrity",
]
