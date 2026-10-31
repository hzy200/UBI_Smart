"""大规模 judge 标注集构建器：规模、校验、去重、切分与校准。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from judge_dataset_builder import (  # noqa: E402
    balance_stats,
    build_cases,
    dedupe_cases,
    split_cases,
    validate_cases,
)

from ubismart.knowledge.judge import deterministic_judgement  # noqa: E402
from ubismart.knowledge.judge_calibration import calibrate_threshold  # noqa: E402


def test_builds_target_size_and_valid_schema() -> None:
    cases = build_cases(1_000)

    assert len(cases) == 1_000
    assert validate_cases(cases) == []
    stats = balance_stats(cases)
    assert stats["total"] == 1_000
    assert set(stats["labels"]) <= {"pass", "fail"}
    assert stats["label_sources"] == {"synthetic": 1_000}


def test_require_human_rejects_synthetic() -> None:
    cases = build_cases(8)

    errors = validate_cases(cases, require_human=True)

    assert errors
    assert all("不是人工标注" in item for item in errors)


def test_dedupe_and_split_are_deterministic() -> None:
    cases = build_cases(100)
    duplicated = cases + cases[:10]

    deduped = dedupe_cases(duplicated)
    split_a = split_cases(deduped)
    split_b = split_cases(list(reversed(deduped)))

    assert len(deduped) == 100
    assert sum(len(items) for items in split_a.values()) == 100
    assert [item["id"] for item in split_a["train"]] == [
        item["id"] for item in split_b["train"]
    ]


def test_large_dataset_calibrates_well() -> None:
    cases = build_cases(1_000)
    samples = [
        {
            "score": deterministic_judgement(
                str(item["answer"]), list(item["sources"])
            )["score"],
            "label": item["label"],
        }
        for item in cases
    ]

    result = calibrate_threshold(samples)

    assert result["samples"] == 1_000
    assert result["accuracy"] >= 0.95
    assert result["f1"] >= 0.9
