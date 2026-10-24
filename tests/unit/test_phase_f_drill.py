"""Phase F：迁移/回滚演练脚本的入口与 SQLite 全流程。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRILL = ROOT / "scripts" / "phase_f_drill.py"
BENCH = ROOT / "scripts" / "phase_f_bench.py"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def test_drill_help() -> None:
    result = _run(DRILL, "--help")
    assert result.returncode == 0, result.stderr
    assert "--mysql-url" in result.stdout


def test_sqlite_drill_upgrade_downgrade_backup_restore() -> None:
    result = _run(DRILL)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    sqlite = report["sqlite"]
    assert sqlite["downgraded"] is True
    assert sqlite["empty_after_reupgrade"] is True
    assert sqlite["restored_title"] == "before"
    assert sqlite["revision"] == "0001_control_schema"


def test_bench_help() -> None:
    result = _run(BENCH, "--help")
    assert result.returncode == 0, result.stderr
    assert "--mysql-url" in result.stdout
