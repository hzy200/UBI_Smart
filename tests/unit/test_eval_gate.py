"""评测结论解读：饱和/退化/不可比三种判定。

TD-006 的教训是"套件饱和了没人知道"。这些用例锁住的是判定口径：
饱和只警告、退化才失败、不可比一律不下结论。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def gate():
    sys.path.insert(0, str(ROOT / "scripts"))
    import eval_gate

    return eval_gate


def _summary(**overrides) -> dict:
    base = {
        "suite": "long",
        "total": 12,
        "passed": 12,
        "pass_rate": 1.0,
        "by_tier": {"long": {"total": 12, "passed": 12}},
        "comparable": True,
    }
    base.update(overrides)
    return base


def test_all_tiers_full_is_saturated(gate) -> None:
    verdict = gate.judge(_summary())
    assert verdict.saturated is True
    assert verdict.saturated_tiers == ["long"]
    assert verdict.failed is False  # 饱和不是失败


def test_mixed_tiers_are_not_saturated(gate) -> None:
    verdict = gate.judge(
        _summary(
            by_tier={
                "long": {"total": 6, "passed": 6},
                "deep": {"total": 6, "passed": 4},
            },
            passed=10,
            pass_rate=10 / 12,
        )
    )
    assert verdict.saturated is False
    assert "deep" not in verdict.saturated_tiers


def test_tiny_suite_is_not_called_saturated(gate) -> None:
    """3 个任务全过说明不了尺子有没有区分度。"""
    verdict = gate.judge(
        _summary(total=3, passed=3, by_tier={"long": {"total": 3, "passed": 3}})
    )
    assert verdict.saturated is False
    assert any("任务数 3" in note for note in verdict.notes)


def test_regression_beyond_threshold_fails(gate) -> None:
    verdict = gate.judge(
        _summary(total=12, passed=8, pass_rate=8 / 12),
        previous={"pass_rate": 11 / 12},
        threshold=0.10,
    )
    assert verdict.regression == pytest.approx(0.25, abs=1e-6)
    assert verdict.failed is True


def test_small_drop_is_reported_but_not_failed(gate) -> None:
    verdict = gate.judge(
        _summary(total=12, passed=11, pass_rate=11 / 12),
        previous={"pass_rate": 1.0},
        threshold=0.10,
    )
    assert verdict.regression == 0.0
    assert verdict.failed is False
    assert any("阈值内" in note for note in verdict.notes)


def test_incomparable_never_fails(gate) -> None:
    """子集运行/脏工作区/沙箱缺 pytest 时，退化也不能算数。"""
    verdict = gate.judge(
        _summary(
            comparable=False,
            comparable_reason="子集运行（--only）",
            total=4,
            passed=2,
            pass_rate=0.5,
        ),
        previous={"pass_rate": 1.0},
    )
    assert verdict.failed is False
    assert any("不可比" in note for note in verdict.notes)


def test_render_marks_the_verdict(gate) -> None:
    ok = gate.render(
        gate.judge(_summary(by_tier={"long": {"total": 12, "passed": 9}}))
    )
    bad = gate.render(
        gate.judge(
            _summary(total=12, passed=6, pass_rate=0.5),
            previous={"pass_rate": 1.0},
        )
    )
    assert ok.startswith("### ✅")
    assert bad.startswith("### ❌")
    assert "退化" in bad


def test_cli_exit_codes(gate, tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(_summary(by_tier={"long": {"total": 12, "passed": 9}})),
        encoding="utf-8",
    )
    assert gate.main([str(good)]) == 0

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_summary(total=12, passed=6, pass_rate=0.5)), "utf-8")
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"pass_rate": 1.0}), encoding="utf-8")
    assert gate.main([str(bad), "--compare", str(prev)]) == 1

    broken = tmp_path / "broken.json"
    broken.write_text("[]", encoding="utf-8")
    assert gate.main([str(broken)]) == 2
