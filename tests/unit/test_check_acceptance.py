"""验收台账的覆盖方式检查：每条都得说明谁来验收。

引用是否存在由 `scripts/check_docs.py` 守着；这里只锁"有没有声明覆盖方式"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def guard():
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_acceptance

    return check_acceptance


def _write(tmp_path: Path, criteria: list[dict]) -> Path:
    import yaml

    path = tmp_path / "criteria.yaml"
    path.write_text(
        yaml.safe_dump(
            {"version": "test", "criteria": criteria}, allow_unicode=True
        ),
        encoding="utf-8",
    )
    return path


def test_clean_ledger_passes(guard, tmp_path) -> None:
    path = _write(
        tmp_path,
        [
            {"id": "REQ-01", "tests": ["tests/unit/test_demo.py::test_alpha"]},
            {"id": "REQ-02", "tests": [], "note": "由脚本承担验收"},
            {"id": "REQ-03", "tests": [], "status": "planned"},
        ],
    )

    errors, stats = guard.check(path)

    assert errors == []
    assert stats["automated"] == 1
    assert stats["declared"] == 1
    assert stats["planned"] == 1
    assert stats["undeclared"] == 0


def test_entry_without_tests_or_note_is_rejected(guard, tmp_path) -> None:
    """静默缺失：读台账的人以为有人守着，实际没有。"""
    path = _write(tmp_path, [{"id": "REQ-01", "tests": []}])

    errors, stats = guard.check(path)

    assert stats["undeclared"] == 1
    assert any("未声明" in item for item in errors)


def test_blank_note_is_not_a_declaration(guard, tmp_path) -> None:
    path = _write(tmp_path, [{"id": "REQ-01", "tests": [], "note": "   "}])

    errors, _ = guard.check(path)

    assert any("未声明" in item for item in errors)


def test_duplicate_id_is_rejected(guard, tmp_path) -> None:
    path = _write(
        tmp_path,
        [
            {"id": "REQ-01", "tests": [], "note": "a"},
            {"id": "REQ-01", "tests": [], "note": "b"},
        ],
    )

    errors, _ = guard.check(path)

    assert any("id 重复" in item for item in errors)


def test_real_ledger_is_clean() -> None:
    """仓库里的真台账必须通过 —— 否则这条规则等于没上线。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_acceptance

    errors, stats = check_acceptance.check()
    assert errors == []
    assert stats["undeclared"] == 0
    assert stats["total"] > 200
