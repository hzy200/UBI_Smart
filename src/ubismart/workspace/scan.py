"""工作区归档扫描（RFC-0001，M3）。

provider：

- `none`：返回 `skipped`，桌面默认；UI 必须明示“未扫描”；
- `clamav`：调用 `clamscan -r`，企业默认；二进制缺失或执行失败时抛
  `ScanUnavailable`，由调用方 fail-closed；
- `remote`：预留，不在本期实现。

扫描只读取 staging，不修改文件；结果写入 revision 的 `scan_status`。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from ubismart.config import Settings


class ScanUnavailable(RuntimeError):
    """扫描器不可用；企业部署应拒绝继续。"""


@dataclass(frozen=True, slots=True)
class ScanReport:
    status: str  # clean | warn | failed | skipped
    detail: str = ""


class Scanner(Protocol):
    def scan(self, root: str) -> ScanReport: ...


class NoopScanner:
    def scan(self, root: str) -> ScanReport:
        return ScanReport(status="skipped", detail="scan disabled")


class ClamavScanner:
    def __init__(
        self, *, binary: str = "clamscan", timeout_s: int = 300
    ) -> None:
        self.binary = binary
        self.timeout_s = max(1, int(timeout_s))

    def scan(self, root: str) -> ScanReport:
        binary = shutil.which(self.binary) or (
            self.binary if "/" in self.binary else ""
        )
        if not binary:
            raise ScanUnavailable("未找到 clamscan")
        try:
            result = subprocess.run(
                [binary, "--no-summary", "-r", str(root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScanUnavailable(
                f"clamscan 执行失败：{type(exc).__name__}: {exc}"
            ) from exc
        output = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0:
            return ScanReport(status="clean")
        if result.returncode == 1:
            return ScanReport(status="warn", detail=output[:2000])
        raise ScanUnavailable(
            f"clamscan 返回 {result.returncode}：{output[:500]}"
        )


def build_scanner(settings: Settings) -> Scanner:
    provider = str(settings.workspace_scan_provider or "none").lower()
    if provider == "clamav":
        return ClamavScanner(
            timeout_s=int(settings.workspace_scan_timeout_s or 300)
        )
    return NoopScanner()
