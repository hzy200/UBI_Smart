"""document / vision / author worker 的结构化链路。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ubismart.config import Settings
from ubismart.orchestrator.document_workers import (
    DocumentAnalysis,
    DocumentMatrix,
    DocumentPoint,
    EvidenceReview,
    SummaryOnly,
    _append_evidence_refs,
    _balanced_sources,
    _context_text,
    _is_plausible_anchor,
    _normalize_citation_paths,
    _resolve_evidence_path,
    _sanitize_citations,
    _split_sections,
    _strip_document_header,
    _verify_source_quotes,
    make_author_runner,
    make_document_runner,
    make_report_review_runner,
    make_vision_runner,
)
from ubismart.tools.artifacts import pack


class _FakeStructured:
    def __init__(self, result: DocumentAnalysis) -> None:
        self.result = result

    async def ainvoke(self, messages: Any, config: Any = None) -> DocumentAnalysis:
        return self.result


class _FakeLLM:
    """同时满足结构化抽取与普通 ainvoke。"""

    def __init__(self, analysis: DocumentAnalysis | None = None) -> None:
        self.analysis = analysis or DocumentAnalysis(summary="摘要")
        self.last_messages: Any = None

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _FakeStructured:
        return _FakeStructured(self.analysis)

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        self.last_messages = messages

        class _Response:
            content = "# 报告\n\n正文"
            usage_metadata = {"input_tokens": 12, "output_tokens": 8}

        return _Response()


class _FakeTool:
    def __init__(self, name: str, reply: str, artifact: dict[str, Any]) -> None:
        self.name = name
        self.reply = reply
        self.artifact = artifact
        self.calls: list[dict[str, Any]] = []

    def invoke(self, args: dict[str, Any]) -> str:
        self.calls.append(args)
        return pack(self.reply, self.artifact)


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, report_output_dir="reports")


def test_document_runner_extracts_points_and_sources(tmp_path) -> None:
    tool = _FakeTool(
        "document_read",
        "[PDF p.1] 关键内容",
        {"kind": "document", "ok": True},
    )
    llm = _FakeLLM(
        DocumentAnalysis(
            summary="一份文档",
            points=[
                DocumentPoint(
                    claim="关键结论",
                    evidence_path="a.pdf",
                    anchor="page=1",
                    quote="关键内容",
                    confidence="high",
                )
            ],
        )
    )
    runner = make_document_runner(llm=llm, tools=[tool], settings=_settings(tmp_path))
    result = asyncio.run(
        runner(
            {
                "id": "doc-1",
                "role": "document",
                "goal": "提炼 a.pdf",
                "inputs": ["a.pdf"],
            },
            parent_request="写报告",
        )
    )
    assert result.status == "success"
    assert result.findings == ["关键结论"]
    assert result.sources[0]["anchor"] == "page=1"
    assert tool.calls[0]["path"] == "a.pdf"


class _PagedTool:
    name = "document_read"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke(self, args: dict[str, Any]) -> str:
        self.calls.append(args)
        if args.get("mode") == "outline":
            return pack(
                "[PDF p.1] outline",
                {"kind": "document", "ok": True, "pages": 12},
            )
        return pack(
            f"[PDF {args.get('page_range', '')}] text",
            {"kind": "document", "ok": True, "pages": 12},
        )


class _MatrixStructured:
    def __init__(self, schema: Any) -> None:
        self.schema = schema

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        text = "\n".join(str(getattr(item, "content", "")) for item in messages)
        if self.schema is DocumentMatrix:
            match = re.search(r"路径：(\S+)", text)
            path = match.group(1) if match else "unknown.pdf"
            return DocumentMatrix(
                path=path,
                title="Paper",
                problem="问题",
                method="方法",
                algorithm="算法",
                experiments="实验",
                limitations="局限",
                innovations="创新",
                conclusion="结论",
            )
        if self.schema is SummaryOnly:
            return SummaryOnly(summary="树状摘要")
        return DocumentAnalysis(summary="片段摘要")


class _MatrixLLM:
    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        return _MatrixStructured(schema)


def test_document_runner_builds_matrix_and_tree_summary(tmp_path) -> None:
    tool = _FakeTool(
        "document_read",
        "[PDF p.1] 内容",
        {"kind": "document", "ok": True, "pages": 1},
    )
    runner = make_document_runner(
        llm=_MatrixLLM(),
        tools=[tool],
        settings=_deep_settings(
            tmp_path,
            document_map_reduce=True,
            document_matrix_enabled=True,
            document_tree_fanout=2,
        ),
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-matrix",
                "role": "document",
                "goal": "总结两篇",
                "inputs": ["a.pdf", "b.pdf"],
            },
            parent_request="总结",
        )
    )

    assert len(result.matrix) == 2
    assert all(item["method"] == "方法" for item in result.matrix)
    assert "树状摘要" in result.summary
    assert "## 证据矩阵" in result.summary
    assert result.verification["matrix_entries"] == 2


def test_document_runner_does_not_truncate_many_inputs(tmp_path) -> None:
    tool = _FakeTool(
        "document_read",
        "[PDF p.1] 内容",
        {"kind": "document", "ok": True, "pages": 1},
    )
    runner = make_document_runner(
        llm=_FakeLLM(), tools=[tool], settings=_settings(tmp_path)
    )
    inputs = [f"p{index}.pdf" for index in range(10)]
    asyncio.run(
        runner(
            {
                "id": "doc-many",
                "role": "document",
                "goal": "读十篇",
                "inputs": inputs,
            },
            parent_request="总结",
        )
    )

    called = {
        call["path"]
        for call in tool.calls
        if call.get("mode") != "outline"
    }
    assert called == set(inputs)


def test_document_runner_reads_long_pdf_in_page_ranges(tmp_path) -> None:
    tool = _PagedTool()
    runner = make_document_runner(
        llm=_FakeLLM(), tools=[tool], settings=_settings(tmp_path)
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-paged",
                "role": "document",
                "goal": "读长文档",
                "inputs": ["long.pdf"],
            },
            parent_request="写报告",
        )
    )
    assert result.status == "success"
    ranges = [
        call.get("page_range")
        for call in tool.calls
        if call.get("mode") != "outline"
    ]
    assert ranges[:3] == ["1-5", "6-10", "11-12"]


def test_normalize_citation_paths_fixes_placeholder_for_single_doc() -> None:
    units = [{"path": "a.pdf", "text": "x"}]
    text = "结论 [来源: 文件#page=3]，另有 [来源: a.pdf#page=1]。"
    text += "锚点带空格 [来源: 文档#page=8 Table 2]，真实路径 [来源: c.pdf#page=5]。"
    fixed = _normalize_citation_paths(text, units, ["a.pdf"])
    assert "[来源: a.pdf#page=3]" in fixed
    assert "[来源: a.pdf#page=1]" in fixed
    assert "[来源: a.pdf#page=8 Table 2]" in fixed
    assert "[来源: c.pdf#page=5]" in fixed
    multi = [{"path": "a.pdf", "text": "x"}, {"path": "b.pdf", "text": "y"}]
    assert _normalize_citation_paths(text, multi, ["a.pdf", "b.pdf"]) == text


def test_strip_document_header_keeps_only_body() -> None:
    text = (
        "文档 pdf/a.pdf（pdf，123 字节，页 3 / 表 0 / 幻灯片 0，"
        "共 10 个内容块）\n请总结\n标题：A\n\n[page=1] 正文内容"
    )
    assert _strip_document_header(text) == "[page=1] 正文内容"
    assert _strip_document_header("[page=1] 正文") == "[page=1] 正文"


def test_balanced_sources_round_robins_across_documents() -> None:
    sources = [
        {"path": path, "claim": f"{path}-{index}"}
        for path in ("a.pdf", "b.pdf", "c.pdf")
        for index in range(10)
    ]
    balanced = _balanced_sources(sources, 6)

    assert [item["path"] for item in balanced] == [
        "a.pdf",
        "b.pdf",
        "c.pdf",
        "a.pdf",
        "b.pdf",
        "c.pdf",
    ]


def test_is_plausible_anchor_rejects_descriptive_placeholders() -> None:
    for anchor in ("page=3", "page=8 Table 2", "outline=5", "Table 1"):
        assert _is_plausible_anchor(anchor)
    for anchor in ("无", "无（内容中未出现页码）", "正文段落", ""):
        assert not _is_plausible_anchor(anchor)


def test_sanitize_citations_keeps_only_known_spans() -> None:
    sources = [
        {"path": "a.pdf", "anchor": "page=1"},
        {"path": "a.pdf", "anchor": "page=8 Table 2"},
    ]
    text = (
        "保留 [来源: a.pdf#page=1]；"
        "粗粒度 [来源: a.pdf#Table 2]；"
        "自造 [来源: a.pdf#SS2D]；"
        "未知路径 [来源: b.pdf#page=1]。"
    )
    cleaned = _sanitize_citations(text, sources)

    assert "[来源: a.pdf#page=1]" in cleaned
    assert "[来源: a.pdf#page=8 Table 2]" in cleaned
    assert "SS2D" not in cleaned
    assert "b.pdf" not in cleaned


def test_sanitize_citations_handles_space_in_path() -> None:
    sources = [{"path": "paper 1.pdf", "anchor": "page=1"}]
    text = (
        "保留 [来源: paper 1.pdf#page=1]；"
        "未知 [来源: WaveFace 摘要#Abstract]。"
    )
    cleaned = _sanitize_citations(text, sources)

    assert "[来源: paper 1.pdf#page=1]" in cleaned
    assert "WaveFace 摘要#Abstract" not in cleaned


def test_sanitize_citations_removes_nested_anchor_markers() -> None:
    sources = [{"path": "a.pdf", "anchor": "page=1"}]
    text = "结论 [来源: a.pdf#[outline=4] VMamba Model] 后续正文"

    cleaned = _sanitize_citations(text, sources)

    assert "[来源:" not in cleaned
    assert "VMamba Model]" not in cleaned
    assert "后续正文" in cleaned


def test_context_text_strips_summary_citations() -> None:
    context = {
        "tasks": [
            {
                "task_id": "t",
                "role": "document",
                "status": "success",
                "summary": "结论 [来源: a.pdf#bad-anchor]",
                "sources": [
                    {"path": "a.pdf", "anchor": "page=1", "claim": "证据"}
                ],
            }
        ]
    }
    text = _context_text(context, max_chars=5_000, source_limit=5)

    assert "bad-anchor" not in text
    assert "a.pdf#page=1" in text


def test_context_text_keeps_every_document() -> None:
    context = {
        "tasks": [
            {
                "task_id": "t",
                "role": "document",
                "status": "success",
                "summary": "s",
                "sources": [
                    {"path": path, "anchor": "page=1", "claim": f"{path}"}
                    for path in ("a.pdf", "b.pdf", "c.pdf")
                ],
            }
        ]
    }
    text = _context_text(context, max_chars=5_000, source_limit=3)

    assert "a.pdf#page=1" in text
    assert "b.pdf#page=1" in text
    assert "c.pdf#page=1" in text


def test_verify_source_quotes_records_span_and_page() -> None:
    units = [
        {
            "path": "a.pdf",
            "text": "[page=1] Intro text here.\n\n"
            "[page=2] Method uses elastic   deformation.",
        }
    ]
    sources = [
        {
            "path": "a.pdf",
            "anchor": "page=2",
            "quote": "elastic deformation",
        },
        {
            "path": "a.pdf",
            "anchor": "page=1",
            "quote": "not present anywhere",
        },
        {
            "path": "a.pdf",
            "anchor": "page=1-2",
            "quote": "Method uses elastic deformation",
        },
        {
            "path": "a.pdf",
            "anchor": "page=2 Table 2",
            "quote": "Method uses elastic deformation",
        },
        {
            "path": "a.pdf",
            "anchor": "page=99",
            "quote": "Method uses elastic deformation",
        },
    ]
    checked = _verify_source_quotes(sources, units)

    assert checked[0]["quote_verified"] is True
    assert checked[0]["quote_span"]["page"] == 2
    assert checked[1]["quote_verified"] is False
    assert checked[2]["anchor"] == "page=2"
    assert checked[2]["anchor_original"] == "page=1-2"
    assert checked[3]["anchor"] == "page=2"
    assert checked[3]["anchor_original"] == "page=2 Table 2"
    assert checked[4]["anchor"] == "page=99"
    assert checked[4]["anchor_valid"] is False


def test_split_sections_keeps_headings_and_page_markers() -> None:
    text = (
        "[page=1] 1 Introduction\nintro text\n\n"
        "[page=2] 2 Method\nmethod text\n\n"
        "2.1 Details\ndetail text\n\n"
        "Figure 1. A caption that is not a section."
    )
    units = _split_sections(text, 200)

    assert units[0].startswith("[page=1] 1 Introduction")
    assert any("2 Method" in unit for unit in units)
    assert any(unit.startswith("2.1 Details") for unit in units)
    assert all("Figure 1" not in unit.splitlines()[0] for unit in units)


def test_split_sections_splits_oversized_section_with_heading() -> None:
    text = "1 Method\n" + ("paragraph body. " * 120)
    units = _split_sections(text, 500)

    assert len(units) > 1
    assert all(unit.startswith("1 Method") for unit in units)


def test_normalize_citation_paths_uses_context_for_multi_doc() -> None:
    units = [
        {"path": "pdf/DPS.pdf", "text": ""},
        {"path": "pdf/VMamba.pdf", "text": ""},
        {"path": "pdf/UNet.pdf", "text": ""},
    ]
    text = (
        "DPS 的 FID [来源: 文档内容#page=7]；"
        "VMamba 的吞吐 [来源: 文档#page=24]；"
        "U-Net 的结果 [来源: 文件#page=6]。"
    )
    fixed = _normalize_citation_paths(text, units, [u["path"] for u in units])

    assert "[来源: pdf/DPS.pdf#page=7]" in fixed
    assert "[来源: pdf/VMamba.pdf#page=24]" in fixed
    assert "[来源: pdf/UNet.pdf#page=6]" in fixed


def test_normalize_citation_paths_falls_back_to_unique_anchor() -> None:
    units = [
        {"path": "pdf/DPS.pdf", "text": ""},
        {"path": "pdf/VMamba.pdf", "text": ""},
    ]
    sources = [{"path": "pdf/DPS.pdf", "anchor": "page=9"}]
    fixed = _normalize_citation_paths(
        "某个结论 [来源: 文档#page=9]", units, [u["path"] for u in units], sources
    )

    assert "[来源: pdf/DPS.pdf#page=9]" in fixed
    unchanged = _normalize_citation_paths(
        "某个结论 [来源: 文档#page=99]", units, [u["path"] for u in units], sources
    )
    assert "[来源: 文档#page=99]" in unchanged


def test_resolve_evidence_path_maps_placeholder_back_to_source() -> None:
    units = [
        {"path": "a.pdf", "text": "alpha quote here"},
        {"path": "b.pdf", "text": "beta quote here"},
    ]
    assert _resolve_evidence_path("a.pdf", "x", units, ["a.pdf", "b.pdf"]) == "a.pdf"
    assert (
        _resolve_evidence_path("文件", "beta quote here", units, ["a.pdf", "b.pdf"])
        == "b.pdf"
    )
    assert _resolve_evidence_path("文件", "not found", units, ["a.pdf", "b.pdf"]) == ""
    assert (
        _resolve_evidence_path("文件", "whatever", [units[0]], ["a.pdf"])
        == "a.pdf"
    )
    # 真实路径即使不在当前文档集合里也不能被改写。
    assert (
        _resolve_evidence_path("c.pdf", "not found", units, ["a.pdf", "b.pdf"])
        == "c.pdf"
    )


def test_append_evidence_refs_adds_only_missing_refs() -> None:
    sources = [
        {"path": "a.pdf", "anchor": "page=1"},
        {"path": "b.pdf", "anchor": "page=2"},
    ]
    summary = _append_evidence_refs(
        "结论 [来源: a.pdf#page=1]", sources
    )

    assert "[来源: a.pdf#page=1]" in summary
    assert "[来源: b.pdf#page=2]" in summary
    assert summary.count("a.pdf#page=1") == 1


def test_append_evidence_refs_is_bounded_and_idempotent() -> None:
    sources = [
        {"path": "a.pdf", "anchor": "page=1"},
        {"path": "a.pdf", "anchor": "page=1"},
        {"path": "a.pdf", "anchor": "page=2"},
        {"path": "", "anchor": "page=3"},
    ]
    summary = _append_evidence_refs("摘要正文", sources)
    assert "a.pdf#page=1" in summary
    assert "a.pdf#page=2" in summary
    assert summary.count("a.pdf#page=1") == 1
    assert _append_evidence_refs(summary, sources) == summary
    assert _append_evidence_refs("摘要", []) == "摘要"


def test_deep_pdf_batches_use_deep_max_chars(tmp_path) -> None:
    tool = _PagedTool()
    runner = make_document_runner(
        llm=_FakeLLM(),
        tools=[tool],
        settings=_deep_settings(
            tmp_path,
            document_map_reduce=True,
            document_max_chars=200_000,
        ),
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-deep",
                "role": "document",
                "goal": "读英文论文",
                "inputs": ["paper.pdf"],
            },
            parent_request="总结",
        )
    )
    auto_calls = [call for call in tool.calls if call.get("mode") != "outline"]
    assert auto_calls
    assert all(call.get("max_chars") == 200_000 for call in auto_calls)
    assert result.coverage["status"] == "complete"


def _deep_settings(tmp_path, **overrides):
    values = {
        "deep_workflows_enabled": True,
        "document_full_coverage": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, report_output_dir="reports", **values)


def test_non_pdf_truncation_marks_coverage_partial(tmp_path) -> None:
    tool = _FakeTool(
        "document_read",
        "only the head of a long document",
        {"kind": "document", "ok": True, "truncated": True},
    )
    runner = make_document_runner(
        llm=_FakeLLM(),
        tools=[tool],
        settings=_deep_settings(
            tmp_path,
            document_map_reduce=True,
            document_max_chars=200_000,
        ),
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-trunc",
                "role": "document",
                "goal": "读长文",
                "inputs": ["long.md"],
            },
            parent_request="总结",
        )
    )

    assert result.status == "partial"
    assert result.coverage["status"] == "partial"
    assert result.coverage["documents"][0]["unread"] == ["tail"]


def test_document_runner_full_coverage_reads_every_page(tmp_path) -> None:
    class _LongPagedTool(_PagedTool):
        def invoke(self, args: dict[str, Any]) -> str:
            self.calls.append(args)
            if args.get("mode") == "outline":
                return pack(
                    "[PDF] outline",
                    {"kind": "document", "ok": True, "pages": 40},
                )
            return pack(
                f"[PDF {args.get('page_range')}] text",
                {"kind": "document", "ok": True, "pages": 40},
            )

    tool = _LongPagedTool()
    runner = make_document_runner(
        llm=_FakeLLM(),
        tools=[tool],
        settings=_deep_settings(tmp_path),
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-full",
                "role": "document",
                "goal": "读全文",
                "inputs": ["long.pdf"],
            },
            parent_request="写报告",
        )
    )

    ranges = [
        call.get("page_range")
        for call in tool.calls
        if call.get("mode") != "outline"
    ]
    assert ranges[-1] == "36-40"
    assert result.coverage["status"] == "complete"
    assert result.status == "success"


def test_document_runner_reports_partial_coverage(tmp_path) -> None:
    class _HugePagedTool(_PagedTool):
        def invoke(self, args: dict[str, Any]) -> str:
            self.calls.append(args)
            if args.get("mode") == "outline":
                return pack(
                    "[PDF] outline",
                    {"kind": "document", "ok": True, "pages": 250},
                )
            return pack(
                f"[PDF {args.get('page_range')}] text",
                {"kind": "document", "ok": True, "pages": 250},
            )

    tool = _HugePagedTool()
    runner = make_document_runner(
        llm=_FakeLLM(),
        tools=[tool],
        settings=_deep_settings(tmp_path, document_max_pages=200),
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-partial",
                "role": "document",
                "goal": "读超大文档",
                "inputs": ["huge.pdf"],
            },
            parent_request="写报告",
        )
    )

    assert result.status == "partial"
    assert result.coverage["status"] == "partial"
    assert result.coverage["documents"][0]["unread"] == ["201-250"]
    assert any(item.startswith("覆盖：") for item in result.findings)


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        outer = self

        class _Structured:
            async def ainvoke(self, messages: Any, config: Any = None):
                outer.calls += 1
                return DocumentAnalysis(
                    summary=f"批次 {outer.calls}",
                    points=[
                        DocumentPoint(
                            claim=f"结论 {outer.calls}",
                            evidence_path="a.pdf",
                            anchor=f"page={outer.calls}",
                            quote="内容",
                        )
                    ],
                )

        return _Structured()


def test_document_runner_map_reduce_merges_batches(tmp_path) -> None:
    tool = _FakeTool(
        "document_read",
        "长文本内容。" * 200,
        {"kind": "document", "ok": True},
    )
    llm = _CountingLLM()
    runner = make_document_runner(
        llm=llm,
        tools=[tool],
        settings=_deep_settings(
            tmp_path,
            document_map_reduce=True,
            document_chunk_chars=500,
            document_map_concurrency=2,
        ),
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-map",
                "role": "document",
                "goal": "提炼长文",
                "inputs": ["a.docx"],
            },
            parent_request="写报告",
        )
    )

    assert llm.calls > 1
    assert "批次" in result.summary
    assert len(result.sources) >= 2
    assert result.status == "success"


class _FakeKnowledgeStore:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []
        self.claims: list[tuple[str, str, list[dict[str, Any]]]] = []

    def save_doc_index(self, run_id: str, doc_id: str, **kwargs: Any) -> str:
        self.docs.append({"run_id": run_id, "doc_id": doc_id, **kwargs})
        return "saved"

    def save_claims(
        self, run_id: str, artifact_id: str, claims: list[dict[str, Any]]
    ) -> str:
        self.claims.append((run_id, artifact_id, claims))
        return "saved"


def test_document_runner_persists_doc_index_and_claims(tmp_path) -> None:
    tool = _FakeTool(
        "document_read",
        "[PDF p.1] 折扣逻辑位于 pricing.py",
        {"kind": "document", "ok": True},
    )
    llm = _FakeLLM(
        DocumentAnalysis(
            summary="一份文档",
            points=[
                DocumentPoint(
                    claim="折扣在 pricing",
                    evidence_path="a.pdf",
                    anchor="page=1",
                    quote="折扣逻辑位于 pricing.py",
                )
            ],
        )
    )
    store = _FakeKnowledgeStore()
    runner = make_document_runner(
        llm=llm,
        tools=[tool],
        settings=_deep_settings(tmp_path),
        knowledge_store=store,
    )
    asyncio.run(
        runner(
            {
                "id": "doc-k",
                "role": "document",
                "goal": "提炼 a.pdf",
                "inputs": ["a.pdf"],
                "knowledge_run_id": "run-1",
            },
            parent_request="写报告",
        )
    )

    assert store.docs[0]["run_id"] == "run-1"
    assert store.docs[0]["doc_id"] == "doc-k"
    assert store.docs[0]["chunks"][0]["path"] == "a.pdf"
    assert store.claims[0][0] == "run-1"
    assert store.claims[0][2][0]["claim"] == "折扣在 pricing"


class _RetrievalStore:
    def search(
        self,
        run_id: str,
        query: str,
        *,
        top_k: int = 8,
        embedder: Any = None,
        mode: str = "lexical",
    ) -> list[dict[str, Any]]:
        return [
            {
                "kind": "claims",
                "path": "a.pdf",
                "anchor": "page=1",
                "text": "检索到的关键证据",
                "score": 1.0,
            }
        ]

    def render_search(self, run_id: str, results: list[dict[str, Any]]) -> str:
        return "[知识检索] 检索到的关键证据"


def test_author_runner_auto_injects_retrieved_evidence(tmp_path) -> None:
    export = _FakeTool(
        "report_export",
        "已生成",
        {"kind": "report", "ok": True, "path": "reports/report.md"},
    )
    llm = _FakeLLM()
    runner = make_author_runner(
        llm=llm,
        tools=[export],
        settings=_deep_settings(tmp_path),
        knowledge_store=_RetrievalStore(),
    )
    asyncio.run(
        runner(
            {
                "id": "author",
                "role": "author",
                "goal": "写报告",
                "output_format": "md",
                "output_path": "reports/report.md",
                "knowledge_run_id": "run-1",
            },
            parent_request="写报告",
            dependency_context={"tasks": []},
        )
    )

    body = llm.last_messages[-1].content
    assert "宿主自动检索到的证据" in body
    assert "检索到的关键证据" in body


def test_author_runner_receives_claim_and_quote_evidence(tmp_path) -> None:
    export = _FakeTool(
        "report_export",
        "已生成",
        {"kind": "report", "ok": True, "path": "reports/report.md"},
    )
    llm = _FakeLLM()
    runner = make_author_runner(
        llm=llm,
        tools=[export],
        settings=Settings(
            _env_file=None,
            report_output_dir="reports",
            deep_workflows_enabled=True,
            author_evidence_max_chars=8_000,
        ),
    )
    asyncio.run(
        runner(
            {
                "id": "author",
                "role": "author",
                "goal": "写报告",
                "output_format": "md",
                "output_path": "reports/report.md",
            },
            parent_request="写报告",
            dependency_context={
                "tasks": [
                    {
                        "task_id": "doc-1",
                        "role": "document",
                        "status": "success",
                        "summary": "摘要",
                        "sources": [
                            {
                                "path": "a.pdf",
                                "anchor": "page=1",
                                "claim": "关键结论",
                                "quote": "原文证据片段",
                            }
                        ],
                    }
                ]
            },
        )
    )

    body = llm.last_messages[-1].content
    assert "关键结论" in body
    assert "原文证据片段" in body


def test_document_runner_understands_embedded_images(tmp_path) -> None:
    read = _FakeTool(
        "document_read",
        "正文",
        {
            "kind": "document",
            "ok": True,
            "pages": 0,
            "image_paths": [".agent/img-1.png"],
        },
    )
    image = _FakeTool(
        "image_inspect",
        "图片内容：柱状图",
        {"kind": "vision", "ok": True},
    )
    runner = make_document_runner(
        llm=_FakeLLM(),
        tools=[read, image],
        settings=_settings(tmp_path),
        image_tool=image,
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-img",
                "role": "document",
                "goal": "读带图文档",
                "inputs": ["a.docx"],
            },
            parent_request="提炼",
        )
    )
    assert result.status == "success"
    assert image.calls[0]["path"] == ".agent/img-1.png"


def test_document_runner_uses_vision_for_scanned_pdf(tmp_path) -> None:
    read = _FakeTool(
        "document_read",
        "[PDF p.1] （无文本层）",
        {"kind": "document", "ok": True, "pages": 2, "needs_ocr": True},
    )
    page = _FakeTool(
        "pdf_page_image",
        "rendered",
        {"kind": "document", "ok": True, "image_path": ".agent/page.png"},
    )
    image = _FakeTool(
        "image_inspect",
        "扫描页文字：发票号码 123",
        {"kind": "vision", "ok": True},
    )
    runner = make_document_runner(
        llm=_FakeLLM(),
        tools=[read, page, image],
        settings=_settings(tmp_path).model_copy(
            update={"document_ocr_enabled": True}
        ),
        image_tool=image,
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-scan",
                "role": "document",
                "goal": "读扫描件",
                "inputs": ["scan.pdf"],
            },
            parent_request="提炼发票",
        )
    )
    assert result.status == "success"
    assert page.calls and image.calls
    assert image.calls[0]["path"] == ".agent/page.png"


def test_document_runner_without_content_fails(tmp_path) -> None:
    tool = _FakeTool(
        "document_read",
        "不支持的格式",
        {"kind": "document", "ok": False},
    )
    runner = make_document_runner(
        llm=_FakeLLM(), tools=[tool], settings=_settings(tmp_path)
    )
    result = asyncio.run(
        runner(
            {
                "id": "doc-2",
                "role": "document",
                "goal": "读取",
                "inputs": ["bad.doc"],
            },
            parent_request="x",
        )
    )
    assert result.status == "failed"
    assert "没有可分析" in result.summary


def test_vision_runner_uses_image_tool(tmp_path) -> None:
    tool = _FakeTool(
        "image_inspect",
        "图中是折线图",
        {"kind": "vision", "ok": True, "findings": ["折线图"]},
    )
    runner = make_vision_runner(tools=[tool])
    result = asyncio.run(
        runner(
            {
                "id": "img-1",
                "role": "vision",
                "goal": "理解图表",
                "inputs": ["a.png"],
            },
            parent_request="x",
        )
    )
    assert result.status == "success"
    assert "折线图" in result.summary
    assert result.sources[0]["anchor"] == "image"


def test_author_runner_exports_report(tmp_path) -> None:
    tool = _FakeTool(
        "report_export",
        "已生成",
        {"kind": "report", "ok": True, "path": "reports/report.md"},
    )
    runner = make_author_runner(
        llm=_FakeLLM(), tools=[tool], settings=_settings(tmp_path)
    )
    result = asyncio.run(
        runner(
            {
                "id": "author",
                "role": "author",
                "goal": "写报告",
                "output_format": "md",
                "output_path": "reports/report.md",
                "template": "reports/template.md",
                "write_scope": ["reports"],
            },
            parent_request="写报告",
            dependency_context={
                "tasks": [
                    {
                        "task_id": "doc-1",
                        "role": "document",
                        "status": "success",
                        "summary": "摘要",
                        "sources": [
                            {"path": "a.pdf", "anchor": "page=1", "quote": "x"}
                        ],
                    }
                ]
            },
        )
    )
    assert result.status == "success"
    assert result.files == ["reports/report.md"]
    assert tool.calls[0]["format"] == "md"
    assert tool.calls[0]["template"] == "reports/template.md"
    assert "a.pdf#page=1" in tool.calls[0]["content"]


def test_author_runner_rejects_output_outside_write_scope(tmp_path) -> None:
    tool = _FakeTool(
        "report_export",
        "不该被调用",
        {"kind": "report", "ok": True, "path": "src/report.md"},
    )
    runner = make_author_runner(
        llm=_FakeLLM(), tools=[tool], settings=_settings(tmp_path)
    )

    result = asyncio.run(
        runner(
            {
                "id": "author",
                "role": "author",
                "goal": "写报告",
                "output_format": "md",
                "output_path": "src/report.md",
                "write_scope": ["reports"],
            },
            parent_request="写报告",
        )
    )

    assert result.status == "failed"
    assert "write_scope" in result.summary
    assert tool.calls == []


def test_author_runner_marks_partial_when_self_check_fails(tmp_path) -> None:
    export = _FakeTool(
        "report_export",
        "已生成",
        {"kind": "report", "ok": True, "path": "reports/report.md"},
    )
    check = _FakeTool(
        "report_check",
        "报告校验失败：空",
        {"kind": "report", "ok": False, "warnings": ["空"]},
    )
    runner = make_author_runner(
        llm=_FakeLLM(), tools=[export, check], settings=_settings(tmp_path)
    )
    result = asyncio.run(
        runner(
            {
                "id": "author",
                "role": "author",
                "goal": "写报告",
                "output_format": "md",
                "output_path": "reports/report.md",
            },
            parent_request="写报告",
        )
    )
    assert result.status == "partial"
    assert "自检" in result.summary


class _ReviewLLM:
    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        class _Structured:
            async def ainvoke(self, messages: Any, config: Any = None):
                return EvidenceReview(
                    supported=False,
                    issues=["第 3 条结论没有引用支撑"],
                    summary="存在无依据结论",
                )

        return _Structured()


def test_report_review_runner_runs_optional_llm_review(tmp_path) -> None:
    check = _FakeTool(
        "report_check", "校验通过", {"kind": "report", "ok": True}
    )
    read = _FakeTool(
        "report_read",
        "# 报告\n\n结论 A",
        {"kind": "report", "ok": True},
    )
    settings = _settings(tmp_path).model_copy(
        update={"report_llm_review": True}
    )
    runner = make_report_review_runner(
        tools=[check, read], llm=_ReviewLLM(), settings=settings
    )
    result = asyncio.run(
        runner(
            {
                "id": "review",
                "role": "review",
                "goal": "校验报告",
                "inputs": ["reports/report.md"],
            },
            parent_request="x",
        )
    )
    assert result.status == "partial"
    assert any("LLM 证据审查未通过" in item for item in result.findings)


def test_report_review_runner_uses_report_check(tmp_path) -> None:
    tool = _FakeTool(
        "report_check",
        "校验通过",
        {"kind": "report", "ok": True},
    )
    runner = make_report_review_runner(tools=[tool])
    result = asyncio.run(
        runner(
            {
                "id": "review",
                "role": "review",
                "goal": "校验报告",
                "inputs": ["reports/report.md"],
            },
            parent_request="x",
        )
    )
    assert result.status == "success"
    assert result.files == ["reports/report.md"]
    assert tool.calls[0]["path"] == "reports/report.md"


def test_dependency_context_respects_total_budget() -> None:
    context = {
        "tasks": [
            {
                "task_id": f"t{index}",
                "role": "document",
                "status": "success",
                "summary": "结论" * 900,
                "findings": [f"要点 {item} " + "x" * 400 for item in range(8)],
                "sources": [
                    {
                        "path": f"doc{index}.pdf",
                        "anchor": f"page={item}",
                        "quote": "q" * 300,
                    }
                    for item in range(20)
                ],
            }
            for index in range(4)
        ]
    }
    text = _context_text(context, max_chars=3_000)
    assert len(text) <= 3_020
    assert "已截断" in text


def test_dependency_context_truncates_long_summary() -> None:
    context = {
        "tasks": [
            {
                "task_id": "t1",
                "role": "document",
                "status": "success",
                "summary": "s" * 5_000,
                "findings": [],
                "sources": [],
            }
        ]
    }
    text = _context_text(context, max_chars=1_000)
    assert len(text) <= 1_020
    assert "s" * 1_500 not in text
