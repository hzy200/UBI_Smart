"""创新点提炼与 novelty critic 的离线自检。"""

from __future__ import annotations

import asyncio
from typing import Any

from ubismart.knowledge.innovation import (
    InnovationCandidate,
    InnovationEvidence,
    InnovationSet,
    NoveltyReview,
    NoveltyReviewSet,
    extract_innovations,
)

_MATRIX = [
    {
        "path": "pdf/a.pdf",
        "title": "Paper A",
        "problem": "长文档证据遗漏",
        "method": "map-reduce",
        "limitations": "后段文档证据不足",
    }
]
_SOURCES = [
    {
        "path": "pdf/a.pdf",
        "anchor": "page=3",
        "claim": "后段文档证据不足",
        "quote": "later documents lose evidence",
        "quote_verified": True,
    }
]


class _FakeStructured:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        return self.result


class _FakeLLM:
    def __init__(self, innovation: InnovationSet, reviews: NoveltyReviewSet):
        self.innovation = innovation
        self.reviews = reviews

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        if schema.__name__ == "InnovationSet":
            return _FakeStructured(self.innovation)
        return _FakeStructured(self.reviews)


def _candidate(title: str, path: str, anchor: str) -> InnovationCandidate:
    return InnovationCandidate(
        title=title,
        gap="后段证据不足",
        insight="需要按文档均衡证据",
        hypothesis="按文档轮转可提升覆盖",
        novelty_claim="按文档均衡的树状归并",
        prior_art_contrast="不同于按分片顺序合并",
        falsifiable_experiment="10 篇时每篇证据数 > 10",
        risks="短文档可能证据更少",
        confidence="medium",
        evidence=[
            InnovationEvidence(
                path=path, anchor=anchor, claim="证据", quote="原文"
            )
        ],
    )


def test_extract_innovations_keeps_only_evidence_backed_candidates() -> None:
    llm = _FakeLLM(
        InnovationSet(
            candidates=[
                _candidate("均衡归并", "pdf/a.pdf", "page=3"),
                _candidate("无证据候选", "pdf/b.pdf", "page=9"),
            ]
        ),
        NoveltyReviewSet(
            reviews=[
                NoveltyReview(
                    title="均衡归并",
                    supported=True,
                    score=82,
                    novelty="medium",
                )
            ]
        ),
    )

    result = asyncio.run(
        extract_innovations(llm, _MATRIX, _SOURCES)
    )

    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["title"] == "均衡归并"
    assert candidate["supported"] is True
    assert candidate["evidence_docs"] == ["pdf/a.pdf"]
    assert any("没有可归属的证据" in issue for issue in result["issues"])


def test_extract_innovations_resolves_basename_evidence_path() -> None:
    llm = _FakeLLM(
        InnovationSet(candidates=[_candidate("相对路径", "a.pdf", "page=3")]),
        NoveltyReviewSet(
            reviews=[
                NoveltyReview(
                    title="相对路径", supported=True, score=75
                )
            ]
        ),
    )

    result = asyncio.run(extract_innovations(llm, _MATRIX, _SOURCES))

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["evidence"][0]["path"] == "pdf/a.pdf"


def test_extract_innovations_requires_matrix_and_sources() -> None:
    result = asyncio.run(
        extract_innovations(_FakeLLM(InnovationSet(), NoveltyReviewSet()), [], [])
    )

    assert result["candidates"] == []
    assert result["issues"]
