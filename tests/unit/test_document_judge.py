"""文档答案 judge：确定性指标与 LLM 回退。"""

from __future__ import annotations

import asyncio
from typing import Any

from ubismart.knowledge.judge import (
    DocumentJudgement,
    deterministic_judgement,
    judge_document_answer,
)

_SOURCES = [
    {
        "path": "a.pdf",
        "anchor": "page=1",
        "quote": "关键原文",
        "claim": "关键结论",
    }
]


def test_deterministic_judge_accepts_known_citation() -> None:
    result = deterministic_judgement(
        "结论见 [来源: a.pdf#page=1]", _SOURCES
    )

    assert result["score"] == 100
    assert result["supported"] is True
    assert result["citation_issues"] == []


def test_deterministic_judge_flags_unknown_citation() -> None:
    result = deterministic_judgement(
        "结论见 [来源: a.pdf#page=9]", _SOURCES
    )

    assert result["supported"] is False
    assert result["citation_issues"] == ["a.pdf#page=9"]
    assert result["score"] < 100


def test_deterministic_judge_accepts_anchor_with_spaces() -> None:
    sources = [
        {"path": "a.pdf", "anchor": "page=8 Table 2", "quote": "表格原文"}
    ]
    result = deterministic_judgement(
        "结论见 [来源: a.pdf#page=8 Table 2]", sources
    )

    assert result["score"] == 100
    assert result["supported"] is True
    assert result["citation_issues"] == []


def test_deterministic_judge_reports_unverified_quote_spans() -> None:
    sources = [
        {
            "path": "a.pdf",
            "anchor": "page=1",
            "quote": "not in the source",
            "quote_verified": False,
        }
    ]
    result = deterministic_judgement(
        "结论见 [来源: a.pdf#page=1]", sources
    )

    assert result["unverified_quotes"] == ["a.pdf#page=1"]
    assert result["quote_verified_ratio"] == 0.0
    assert result["score"] < 100


def test_deterministic_judge_accepts_coarser_anchor_variants() -> None:
    sources = [
        {
            "path": "a.pdf",
            "anchor": "page=8 Table 2",
            "quote": "表格原文",
        }
    ]
    for answer in (
        "结论见 [来源: a.pdf#page=8]",
        "结论见 [来源: a.pdf#Table 2]",
    ):
        result = deterministic_judgement(answer, sources)
        assert result["supported"] is True, answer
        assert result["citation_issues"] == []


def test_deterministic_judge_does_not_penalize_duplicate_anchors() -> None:
    sources = [
        {"path": "a.pdf", "anchor": "page=1", "quote": "一"},
        {"path": "a.pdf", "anchor": "page=1", "quote": "二"},
    ]
    result = deterministic_judgement("结论见 [来源: a.pdf#page=1]", sources)

    assert result["score"] == 100
    assert result["supported"] is True


def test_deterministic_judge_requires_inline_citation_when_evidence_exists() -> None:
    result = deterministic_judgement("结论没有任何行内引用", _SOURCES)

    assert result["score"] == 40
    assert result["supported"] is False


def test_deterministic_judge_without_sources_is_unsupported() -> None:
    result = deterministic_judgement("没有证据的结论", [])

    assert result["score"] == 0
    assert result["supported"] is False


class _FakeStructured:
    def __init__(self, result: Any = None, error: Exception | None = None):
        self.result = result
        self.error = error

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


class _FakeLLM:
    def __init__(self, structured: _FakeStructured) -> None:
        self.structured = structured

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        return self.structured


def test_llm_judge_returns_model_score() -> None:
    llm = _FakeLLM(
        _FakeStructured(
            DocumentJudgement(
                score=88,
                supported=True,
                summary="全部有证据",
            )
        )
    )

    result = asyncio.run(
        judge_document_answer(
            llm, "问题", "答案 a.pdf#page=1", _SOURCES
        )
    )

    assert result["mode"] == "llm"
    assert result["score"] == 88
    assert result["supported"] is True
    assert result["deterministic_score"] == 100


def test_llm_judge_failure_falls_back_to_deterministic() -> None:
    llm = _FakeLLM(_FakeStructured(error=RuntimeError("down")))

    result = asyncio.run(
        judge_document_answer(
            llm, "问题", "答案 a.pdf#page=1", _SOURCES
        )
    )

    assert result["mode"] == "deterministic"
    assert result["error"]
    assert result["supported"] is True
