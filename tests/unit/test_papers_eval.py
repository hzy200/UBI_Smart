"""论文联合评测样本与判定的离线自检（不依赖固定三篇）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from papers import (  # noqa: E402
    PDF_DIR,
    REPORT_SECTIONS,
    build_cases,
    case_inputs,
    discover_papers,
    evaluate_report_case,
    evaluate_result,
)

from ubismart.orchestrator.contracts import WorkerResult  # noqa: E402


def _paper(paper_id: str, index: int) -> dict[str, object]:
    return {
        "id": paper_id,
        "source": f"/tmp/paper-{index}.pdf",
        "workspace": f"Paper{index}.pdf",
        "title": paper_id,
        "pages": 1,
        "known": paper_id in {"unet", "vmamba", "dps"},
    }


def _literal(pattern: str) -> str:
    return pattern.replace("\\", "")


def _fake_result(case, *, drop_doc: str = "") -> WorkerResult:
    inputs = [path for path in case_inputs(case) if path != drop_doc]
    answer = " ".join(
        _literal(pattern)
        for group in case.get("concepts") or []
        for pattern in group["any"][: int(group.get("min_hits", 1))]
    )
    answer += " " + " ".join(f"[来源: {path}#page=1]" for path in inputs)
    sources = [
        {
            "path": path,
            "anchor": "page=1",
            "quote": "证据原文",
            "claim": "结论",
            "quote_verified": True,
            "quote_span": {"start": 0, "end": 4, "page": 1},
        }
        for path in inputs
        for _ in range(3)
    ]
    matrix = [
        {
            "path": path,
            "title": Path(str(path)).stem,
            "problem": "问题",
            "method": "方法",
            "algorithm": "算法",
            "experiments": "实验",
            "limitations": "局限",
            "innovations": "创新",
            "conclusion": "结论",
            "evidence": [],
        }
        for path in case_inputs(case)
    ]
    return WorkerResult(
        task_id=case["id"],
        role="document",
        status="success",
        summary=answer,
        sources=sources,
        matrix=matrix,
        attribution_issues=[],
        coverage={
            "status": "complete",
            "documents": [
                {
                    "path": path,
                    "pages": 1,
                    "read_ranges": ["1"],
                    "unread": [],
                    "status": "complete",
                }
                for path in case_inputs(case)
            ],
        },
        input_tokens=10,
        output_tokens=5,
        duration_ms=100,
    )


def test_cases_scale_with_paper_count() -> None:
    two = [_paper("unet", 1), _paper("vmamba", 2)]
    five = [
        _paper("unet", 1),
        _paper("vmamba", 2),
        _paper("dps", 3),
        _paper("paper4", 4),
        _paper("paper5", 5),
    ]
    for papers in (two, five):
        cases = build_cases(papers)
        ids = {case["id"] for case in cases}
        assert "papers/cross_method" in ids
        assert "papers/cross_limits" in ids
        for case in cases:
            assert case["papers"] == papers
            assert len(case_inputs(case)) == len(papers)
            if case.get("kind") == "report":
                assert case["required_sections"]
                assert case["min_report_chars"] > 0
            else:
                assert case["max_answer_chars"] > 0
            for group in case.get("concepts") or []:
                assert group["any"]
                assert int(group.get("min_hits", 1)) >= 1


def test_cases_without_known_papers_skip_number_case() -> None:
    papers = [_paper("paper1", 1), _paper("paper2", 2)]
    cases = build_cases(papers)
    ids = {case["id"] for case in cases}

    assert ids == {
        "papers/cross_method",
        "papers/cross_limits",
        "papers/report",
    }
    method = next(case for case in cases if case["id"] == "papers/cross_method")
    assert [group["name"] for group in method["concepts"]] == ["comparison"]


def test_evaluate_result_passes_for_different_document_counts() -> None:
    for papers in (
        [_paper("unet", 1), _paper("vmamba", 2)],
        [
            _paper("unet", 1),
            _paper("vmamba", 2),
            _paper("dps", 3),
            _paper("paper4", 4),
        ],
    ):
        for case in build_cases(papers):
            report = evaluate_result(_fake_result(case), case)
            assert report["passed"], report["reasons"]
            assert report["coverage_complete"] is True
            assert len(report["cited_expected_docs"]) == len(
                report["required_docs"]
            )
            assert report["judgement"]["supported"] is True


def _report_text(case) -> str:
    body = "\n\n".join(
        f"## {name}\n" + "内容。" * 80 for name in REPORT_SECTIONS
    )
    refs = " ".join(
        f"[来源: {path}#page=1]"
        for path in case.get("required_cited_docs") or []
    )
    return body + "\n\n" + refs


def _author_result() -> WorkerResult:
    return WorkerResult(
        task_id="author",
        role="author",
        status="success",
        summary="已生成报告",
        output_path="reports/paper-report.md",
        files=["reports/paper-report.md"],
        input_tokens=10,
        output_tokens=5,
        duration_ms=10,
    )


def _review_result(*, ok: bool = True) -> WorkerResult:
    return WorkerResult(
        task_id="review",
        role="review",
        status="success" if ok else "partial",
        findings=[
            "reports/paper-report.md 校验通过"
            if ok
            else "reports/paper-report.md: 缺少必需章节：创新"
        ],
    )


def test_report_case_and_evaluation() -> None:
    papers = [_paper("unet", 1), _paper("vmamba", 2), _paper("dps", 3)]
    case = next(
        item for item in build_cases(papers)
        if item["id"] == "papers/report"
    )
    assert case["kind"] == "report"
    assert case["required_sections"] == REPORT_SECTIONS

    report = evaluate_report_case(
        case,
        _fake_result(case),
        _author_result(),
        _review_result(),
        _report_text(case),
    )

    assert report["passed"], report["reasons"]
    assert all(report["sections"].values())
    assert len(report["cited_expected_docs"]) == len(case["papers"])


def test_report_case_flags_missing_section_and_failed_check() -> None:
    papers = [_paper("unet", 1), _paper("vmamba", 2)]
    case = next(
        item for item in build_cases(papers)
        if item["id"] == "papers/report"
    )
    text = _report_text(case).replace("## 创新", "")
    report = evaluate_report_case(
        case, _fake_result(case), _author_result(), _review_result(ok=False), text
    )

    assert report["passed"] is False
    assert any("缺少章节" in reason for reason in report["reasons"])
    assert any("report_check 未通过" in reason for reason in report["reasons"])


def test_number_case_requires_only_asked_known_docs() -> None:
    papers = [
        _paper("unet", 1),
        _paper("vmamba", 2),
        _paper("dps", 3),
        _paper("paper4", 4),
    ]
    case = next(
        item for item in build_cases(papers)
        if item["id"] == "papers/cross_numbers"
    )
    extra = case_inputs(case)[3]
    assert extra not in case["required_cited_docs"]

    result = _fake_result(case)
    result.summary = result.summary.replace(f"[来源: {extra}#page=1]", "")
    result.sources = [
        source for source in result.sources if source["path"] != extra
    ]
    report = evaluate_result(result, case)

    assert report["passed"], report["reasons"]
    assert extra not in report["cited_expected_docs"]


def test_evaluate_result_allows_covered_page_without_extracted_point() -> None:
    papers = [_paper("unet", 1), _paper("vmamba", 2)]
    case = build_cases(papers)[0]
    result = _fake_result(case)
    target = case_inputs(case)[0]
    result.sources = [
        source for source in result.sources if source["path"] != target
    ] + [{"path": target, "anchor": "page=2", "quote": "q", "claim": "c"}] * 3
    result.summary = result.summary.replace(
        f"[来源: {target}#page=1]", f"[来源: {target}#page=7]"
    )
    for document in result.coverage["documents"]:
        document["pages"] = 10
        document["read_ranges"] = ["1-10"]

    report = evaluate_result(result, case)

    assert report["passed"], report["reasons"]
    assert report["citation_warnings"] == [f"{target}#page=7"]
    assert report["judgement"]["supported"] is False


def test_evaluate_result_rejects_page_outside_coverage() -> None:
    papers = [_paper("unet", 1), _paper("vmamba", 2)]
    case = build_cases(papers)[0]
    result = _fake_result(case)
    target = case_inputs(case)[0]
    result.sources = [
        source for source in result.sources if source["path"] != target
    ] + [{"path": target, "anchor": "page=2", "quote": "q", "claim": "c"}] * 3
    result.summary = result.summary.replace(
        f"[来源: {target}#page=1]", f"[来源: {target}#page=99]"
    )
    for document in result.coverage["documents"]:
        document["pages"] = 10
        document["read_ranges"] = ["1-10"]

    report = evaluate_result(result, case)

    assert report["passed"] is False
    assert any("judge 未通过" in reason for reason in report["reasons"])
    assert report["citation_warnings"] == []


def test_evaluate_result_flags_attribution_issue() -> None:
    papers = [_paper("unet", 1), _paper("vmamba", 2)]
    case = build_cases(papers)[0]
    result = _fake_result(case)
    result.attribution_issues = [
        {
            "mentioned": ["pdf/Paper1.pdf"],
            "cited": ["pdf/Paper2.pdf"],
            "text": "错配",
        }
    ]
    report = evaluate_result(result, case)

    assert report["passed"] is False
    assert any("归属错配" in reason for reason in report["reasons"])


def test_evaluate_result_flags_sparse_matrix() -> None:
    papers = [_paper("unet", 1), _paper("vmamba", 2)]
    case = build_cases(papers)[0]
    result = _fake_result(case)
    result.matrix = []
    report = evaluate_result(result, case)

    assert report["passed"] is False
    assert any("证据矩阵槽位不足" in reason for reason in report["reasons"])


def test_evaluate_result_flags_missing_document_citation() -> None:
    papers = [_paper("unet", 1), _paper("vmamba", 2), _paper("dps", 3)]
    case = build_cases(papers)[0]
    missing = case_inputs(case)[-1]
    report = evaluate_result(_fake_result(case, drop_doc=missing), case)

    assert report["passed"] is False
    assert any("未引用要求的文档" in reason for reason in report["reasons"])
    assert any(missing in reason for reason in report["reasons"])


def test_evaluate_result_flags_missing_concepts() -> None:
    papers = [_paper("unet", 1), _paper("vmamba", 2)]
    case = build_cases(papers)[0]
    result = _fake_result(case)
    result.summary = " ".join(
        f"[来源: {path}#page=1]" for path in case_inputs(case)
    )
    report = evaluate_result(result, case)

    assert report["passed"] is False
    assert any("缺少概念" in reason for reason in report["reasons"])


def test_discover_papers_recognizes_known_and_unknown_files(tmp_path) -> None:
    (tmp_path / "A.pdf").write_bytes(b"")
    (tmp_path / "U-Net.pdf").write_bytes(b"")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")

    papers = discover_papers(tmp_path)

    assert [paper["title"] for paper in papers] == ["A", "U-Net"]
    assert [paper["id"] for paper in papers] == ["paper1", "unet"]
    assert papers[1]["workspace"] == "UNet.pdf"


def test_paper_sources_exist_when_pdf_folder_is_present() -> None:
    papers = discover_papers(PDF_DIR)
    if not papers:
        pytest.skip("未提供 pdf/ 语料目录")
    for paper in papers:
        assert Path(str(paper["source"])).is_file()
