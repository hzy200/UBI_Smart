"""judge 阈值校准与可配置 pass score。"""

from __future__ import annotations

from ubismart.knowledge.judge import deterministic_judgement
from ubismart.knowledge.judge_calibration import calibrate_threshold


def test_calibration_finds_a_separating_threshold() -> None:
    samples = [
        {"score": 95, "label": "pass"},
        {"score": 88, "label": "pass"},
        {"score": 40, "label": "fail"},
        {"score": 12, "label": "fail"},
    ]

    result = calibrate_threshold(samples)

    assert 41 <= result["threshold"] <= 88
    assert result["accuracy"] == 1.0
    assert result["f1"] == 1.0


def test_calibration_without_samples_uses_default() -> None:
    result = calibrate_threshold([], default=55)

    assert result["threshold"] == 55
    assert result["samples"] == 0


def test_calibration_skips_bad_rows() -> None:
    result = calibrate_threshold(
        [
            {"score": 90, "label": "pass"},
            {"score": "bad", "label": "pass"},
            {"score": 10, "label": "unknown"},
            "not-a-dict",
        ]
    )

    assert result["samples"] == 1
    assert result["skipped"] == 3


def test_pass_score_can_be_configured() -> None:
    # 有 anchor、无 quote：score=60；把阈值抬到 80 应判不支持。
    sources = [{"path": "a.pdf", "anchor": "page=1"}]
    result = deterministic_judgement(
        "结论 a.pdf#page=1", sources, pass_score=80
    )

    assert result["score"] == 60
    assert result["supported"] is False
    assert result["pass_score"] == 80
