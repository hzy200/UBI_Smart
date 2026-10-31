"""长文档效果指标的确定性部分。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from doc_metrics import (  # noqa: E402
    citation_metrics,
    coverage_metrics,
    evidence_coverage,
    summarize_document_run,
)


def test_coverage_metrics_expands_ranges() -> None:
    metrics = coverage_metrics(
        {
            "status": "partial",
            "documents": [
                {
                    "path": "a.pdf",
                    "pages": 10,
                    "read_ranges": ["1-5", "7-8"],
                    "unread": ["6", "9-10"],
                }
            ],
        }
    )

    assert metrics["status"] == "partial"
    assert metrics["total_pages"] == 10
    assert metrics["read_pages"] == 7
    assert metrics["coverage_ratio"] == 0.7
    assert metrics["unread_ranges"] == ["6", "9-10"]


def test_citation_metrics_counts_anchors_and_quotes() -> None:
    metrics = citation_metrics(
        [
            {"path": "a.pdf", "anchor": "page=1", "quote": "原文"},
            {"path": "a.pdf", "anchor": "", "quote": ""},
        ]
    )

    assert metrics["citations"] == 2
    assert metrics["with_anchor"] == 1
    assert metrics["anchor_ratio"] == 0.5


def test_evidence_coverage_matches_path_and_anchor() -> None:
    metrics = evidence_coverage(
        [
            {"evidence_path": "a.pdf", "anchor": "page=1"},
            {"evidence_path": "a.pdf", "anchor": "page=2"},
        ],
        [{"path": "a.pdf", "anchor": "page=1"}],
    )

    assert metrics["claims"] == 2
    assert metrics["supported"] == 1
    assert metrics["coverage_ratio"] == 0.5


def test_summarize_document_run_combines_metrics() -> None:
    summary = summarize_document_run(
        {
            "task_id": "d1",
            "role": "document",
            "status": "partial",
            "coverage": {
                "status": "partial",
                "documents": [
                    {"pages": 4, "read_ranges": ["1-2"], "unread": ["3-4"]}
                ],
            },
            "sources": [{"path": "a", "anchor": "p1", "quote": "q"}],
        }
    )

    assert summary["coverage"]["coverage_ratio"] == 0.5
    assert summary["citations"]["anchor_ratio"] == 1.0
