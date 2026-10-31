"""judge 种子标注集与校准结果的自检。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from judge_dataset import JUDGE_CASES, as_jsonl  # noqa: E402

from ubismart.knowledge.judge import deterministic_judgement  # noqa: E402
from ubismart.knowledge.judge_calibration import calibrate_threshold  # noqa: E402


def test_seed_dataset_has_expandable_shape() -> None:
    assert len(JUDGE_CASES) >= 24
    ids = {item["id"] for item in JUDGE_CASES}
    assert len(ids) == len(JUDGE_CASES)
    for item in JUDGE_CASES:
        assert item["label"] in {"pass", "fail"}
        assert item["question"] and item["answer"]
        assert isinstance(item["sources"], list)


def test_seed_dataset_can_be_exported_as_jsonl() -> None:
    lines = [line for line in as_jsonl().splitlines() if line]

    assert len(lines) == len(JUDGE_CASES)
    assert json.loads(lines[0])["id"] == JUDGE_CASES[0]["id"]


def test_seed_dataset_calibrates_to_a_useful_threshold() -> None:
    samples = [
        {
            "score": deterministic_judgement(
                str(item["answer"]), list(item["sources"])
            )["score"],
            "label": item["label"],
        }
        for item in JUDGE_CASES
    ]

    result = calibrate_threshold(samples)

    assert result["samples"] == len(JUDGE_CASES)
    assert result["accuracy"] >= 0.8
    assert result["f1"] >= 0.8
    assert 0 <= result["threshold"] <= 100
