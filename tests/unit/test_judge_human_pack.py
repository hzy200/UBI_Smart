"""待人工审核的 judge 标注包：数量、强制字段与提升流程。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from judge_dataset_builder import validate_cases  # noqa: E402
from judge_human_pack import (  # noqa: E402
    build_pending_pack,
    promote_case,
)


def test_pending_pack_is_large_and_not_human_yet() -> None:
    cases = build_pending_pack(150)

    assert len(cases) == 150
    assert all(item["label"] == "" for item in cases)
    assert all(
        item["label_source"] == "pending-human" for item in cases
    )
    # 空 label 不能进入正式标注集。
    assert validate_cases(cases)
    assert validate_cases(cases, require_human=True)


def test_promote_requires_label_and_reviewer() -> None:
    case = build_pending_pack(1)[0]

    with pytest.raises(ValueError):
        promote_case(case, label="maybe", reviewer="alice", reviewed_at="x")
    with pytest.raises(ValueError):
        promote_case(case, label="pass", reviewer="", reviewed_at="x")

    promoted = promote_case(
        case, label="pass", reviewer="alice", reviewed_at="2026-10-31"
    )
    assert promoted["label_source"] == "human"
    assert promoted["reviewer"] == "alice"
    assert validate_cases([promoted], require_human=True) == []
