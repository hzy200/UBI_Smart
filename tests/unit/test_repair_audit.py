"""审计修复工具：隔离坏行，恢复可重复验证。"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from repair_audit import main, repair_file  # noqa: E402

from ubismart.audit import (  # noqa: E402
    AuditLogger,
    AuditRecord,
    verify_audit_files,
)

BAD = b'_ms":362,"steps":[],"detail":"","path":"","action":""\n'


def _chain(directory: Path) -> Path:
    logger = AuditLogger(directory)
    for kind in ("run_start", "run_end"):
        logger.write(
            AuditRecord(
                ts="2026-01-01T00:00:00+00:00",
                kind=kind,
                thread_id="t",
            )
        )
    return logger.files_today()[0]


def test_dry_run_does_not_modify(tmp_path: Path) -> None:
    path = _chain(tmp_path)
    before = path.read_bytes() + BAD
    path.write_bytes(before)

    result = repair_file(path, write=False)

    assert len(result["bad"]) == 1
    assert result["written"] is False
    assert path.read_bytes() == before


def test_write_quarantines_and_restores_chain(tmp_path: Path) -> None:
    path = _chain(tmp_path)
    before = path.read_bytes() + BAD
    path.write_bytes(before)

    result = repair_file(path, write=True)

    assert result["written"] is True
    assert len(result["bad"]) == 1
    assert BAD.strip() not in path.read_bytes()
    corrupt = path.with_suffix(path.suffix + ".corrupt")
    assert corrupt.exists()
    assert "_ms" in corrupt.read_text(encoding="utf-8")
    ok, message = verify_audit_files([path])
    assert ok, message


def test_clean_file_is_untouched(tmp_path: Path) -> None:
    path = _chain(tmp_path)
    before = path.read_bytes()

    result = repair_file(path, write=True)

    assert result["bad"] == []
    assert result["written"] is False
    assert path.read_bytes() == before


def test_cli_dry_run_fails_on_bad_lines(tmp_path: Path) -> None:
    path = _chain(tmp_path)
    path.write_bytes(path.read_bytes() + BAD)

    assert main(["--dir", str(tmp_path), "--json"]) == 1


def test_cli_write_repairs(tmp_path: Path) -> None:
    path = _chain(tmp_path)
    path.write_bytes(path.read_bytes() + BAD)

    assert main(["--dir", str(tmp_path), "--write", "--json"]) == 0
    ok, message = verify_audit_files([path])
    assert ok, message
