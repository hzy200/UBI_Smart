"""Phase D5：smoke 脚本的轻量契约。

完整故障演练会启动多个子进程，放在 `scripts/phase_d_smoke.py` 手动/发版前
运行；这里只锁脚本入口与辅助函数，避免快反馈被进程启动拖慢。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "phase_d_smoke.py"


def _load():
    spec = importlib.util.spec_from_file_location("phase_d_smoke", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_lists_failure_drill_options() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--keep" in result.stdout
    assert "--timeout" in result.stdout


def test_free_ports_are_distinct_and_bindable() -> None:
    module = _load()
    ports = module._free_ports(2)
    assert len(ports) == 2
    assert ports[0] != ports[1]


def test_clean_env_removes_internal_overrides(monkeypatch) -> None:
    module = _load()
    monkeypatch.setenv("AGENT_INTERNAL_URL", "http://example.invalid")
    monkeypatch.setenv("AGENT_CONTROL_DB_URL", "sqlite:///x.db")
    env = module._clean_env()
    assert "AGENT_INTERNAL_URL" not in env
    assert "AGENT_CONTROL_DB_URL" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


def test_script_is_valid_python() -> None:
    if not shutil.which(sys.executable):
        return
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
