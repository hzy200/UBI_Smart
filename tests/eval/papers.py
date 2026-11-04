"""多篇英文论文的联合阅读评测：样本、跑法与判定。

文件数量不固定：运行时扫描 `pdf/`（或 `--pdf-dir`）下的全部 PDF，
为已知论文附加概念/数值要点，未知 PDF 仍参与覆盖与引用检查。
新增/删除 PDF 后不需要改测试代码。

用法：
    python tests/eval/papers.py --list
    python tests/eval/papers.py --case papers/cross_method --json
    python tests/eval/papers.py --all --judge-llm
    python tests/eval/papers.py --pdf-dir /path/to/pdfs --all --json

需要真实 WSL + `DEEPSEEK_API_KEY`（或 `AGENT_LLM_PROVIDERS`）。
pytest 入口见 `tests/integration/test_paper_documents.py`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PDF_DIR = ROOT / "pdf"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

_REF_RE = re.compile(r"\[来源\s*[:：]\s*([^\]#]+?)#([^\]]+?)\s*\]")
MATRIX_SLOT_KEYS = (
    "problem",
    "method",
    "algorithm",
    "experiments",
    "limitations",
    "innovations",
    "conclusion",
)

# 已知论文的稳定元数据；按文件名（小写）匹配。未命中的 PDF 自动生成
# paper<N> 标识，仍然参与覆盖、引用与上下文管理检查。
KNOWN_PAPERS: dict[str, dict[str, Any]] = {
    "u-net.pdf": {
        "id": "unet",
        "workspace": "UNet.pdf",
        "title": "U-Net",
        "pages": 8,
    },
    "vmamba.pdf": {
        "id": "vmamba",
        "workspace": "VMamba.pdf",
        "title": "VMamba",
        "pages": 25,
    },
    "diffusion posterior sampling for general noisy inverse problems.pdf": {
        "id": "dps",
        "workspace": "DPS.pdf",
        "title": "DPS",
        "pages": 30,
    },
}

# 方法对比用例中，已知论文各自必须出现的关键词（命中 min_hits 个即可）。
METHOD_CONCEPTS: dict[str, dict[str, Any]] = {
    "unet": {
        "name": "unet",
        "any": ["全卷积", "收缩路径", "扩张路径", "跳跃连接", "生物医学"],
        "min_hits": 2,
    },
    "vmamba": {
        "name": "vmamba",
        "any": [
            "状态空间",
            "SSM",
            "选择性扫描",
            "SS2D",
            "Cross-Scan",
            "线性复杂度",
        ],
        "min_hits": 2,
    },
    "dps": {
        "name": "dps",
        "any": [
            "后验采样",
            "后验",
            "含噪",
            "噪声逆问题",
            "Tweedie",
            "x0",
            "近似似然",
        ],
        "min_hits": 2,
    },
}

# 数值检索用例中，已知论文各自的量化结果。
NUMBER_CONCEPTS: dict[str, dict[str, Any]] = {
    "unet": {
        "name": "unet",
        "any": ["warping error", r"0\.00035", "rand error"],
        "min_hits": 2,
    },
    "vmamba": {
        "name": "vmamba",
        "any": [r"82\.6", "1686", "ImageNet"],
        "min_hits": 2,
    },
    "dps": {
        "name": "dps",
        "any": [r"39\.35", "FID", "FFHQ"],
        "min_hits": 2,
    },
}

REPORT_SECTIONS = ["摘要", "方法", "实验", "局限", "创新"]

NUMBER_PROMPTS: dict[str, str] = {
    "unet": "U-Net 的 warping error",
    "vmamba": "VMamba-T 的 ImageNet top-1 与吞吐",
    "dps": "DPS 在 FFHQ SR×4 上的 FID",
}

# 边界/组合用例中，已知论文各自的限制条件。
LIMIT_CONCEPTS: dict[str, dict[str, Any]] = {
    "unet": {
        "name": "unet",
        "any": ["少样本", "标注", "稀缺", "像素级", "分割"],
        "min_hits": 2,
    },
    "vmamba": {
        "name": "vmamba",
        "any": [
            "吞吐",
            "内存",
            "工程",
            "实现",
            "大规模",
            "预训练",
            "OOM",
            "高分辨率",
        ],
        "min_hits": 2,
    },
    "dps": {
        "name": "dps",
        "any": ["采样慢", r"78\.52", "NFE", "PSNR", "感知", "非唯一"],
        "min_hits": 2,
    },
}


def discover_papers(pdf_dir: Path | str = PDF_DIR) -> list[dict[str, Any]]:
    """扫描目录下的 PDF；文件名变化只影响标识，不影响用例结构。"""
    directory = Path(pdf_dir)
    if not directory.is_dir():
        return []
    files = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        ),
        key=lambda path: path.name.lower(),
    )
    papers: list[dict[str, Any]] = []
    for index, path in enumerate(files, 1):
        known = KNOWN_PAPERS.get(path.name.lower(), {})
        papers.append(
            {
                "id": str(known.get("id") or f"paper{index}"),
                "source": str(path),
                "workspace": str(
                    known.get("workspace") or f"paper-{index}.pdf"
                ),
                "title": str(known.get("title") or path.stem),
                "pages": int(known.get("pages") or 0),
                "known": bool(known),
            }
        )
    return papers


def _max_chars(kind: str, count: int) -> int:
    if kind == "numbers":
        return min(8000, 1200 * count)
    return min(24_000, 2000 * count)


def build_cases(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按当前 PDF 列表生成用例；数量变化时自动扩展/收缩。"""
    if not papers:
        return []
    count = len(papers)
    ids = {str(paper["id"]) for paper in papers}

    method_concepts = [
        dict(METHOD_CONCEPTS[paper_id])
        for paper_id in sorted(ids)
        if paper_id in METHOD_CONCEPTS
    ]
    method_concepts.append(
        {
            "name": "comparison",
            "any": ["三者", "三篇", "对比", "各自", "分别", "差异", "组合"],
            "min_hits": 1,
        }
    )
    cases: list[dict[str, Any]] = [
        {
            "id": "papers/cross_method",
            "papers": papers,
            "question": (
                f"请联合阅读提供的 {count} 篇论文并用中文回答："
                "1) 各篇要解决的核心问题；"
                "2) 从“先验/骨干/求解器”的角度比较它们的核心方法；"
                "3) 它们之间是否存在可组合或相互借鉴的关系？"
                "每个关键结论都要附 [来源: 文件#锚点]，且每篇论文都必须被引用。"
            ),
            "concepts": method_concepts,
            "require_all_docs_cited": True,
            "min_sources_per_doc": 1,
            "min_matrix_slot_ratio": 0.4,
            "max_attribution_issues": 0,
            "max_answer_chars": _max_chars("method", count),
        }
    ]

    number_ids = [paper_id for paper_id in sorted(ids) if paper_id in NUMBER_CONCEPTS]
    if number_ids:
        required = [
            f"pdf/{paper['workspace']}"
            for paper in papers
            if str(paper["id"]) in number_ids
        ]
        cases.append(
            {
                "id": "papers/cross_numbers",
                "papers": papers,
                "question": (
                    "请从提供的论文中分别找出并解释以下关键量化结果："
                    + "、".join(NUMBER_PROMPTS[paper_id] for paper_id in number_ids)
                    + "。每个数字都必须标注来源，并说明它衡量的是什么；"
                    "被问到的论文都必须被引用。"
                ),
                "concepts": [
                    dict(NUMBER_CONCEPTS[paper_id])
                    for paper_id in number_ids
                ],
                "required_cited_docs": required,
                "require_all_docs_cited": True,
                "min_sources_per_doc": 1,
                "min_matrix_slot_ratio": 0.4,
                "max_attribution_issues": 0,
                "max_answer_chars": _max_chars("numbers", count),
            }
        )

    limit_concepts = [
        dict(LIMIT_CONCEPTS[paper_id])
        for paper_id in sorted(ids)
        if paper_id in LIMIT_CONCEPTS
    ]
    limit_concepts.append(
        {
            "name": "synthesis",
            "any": ["组合", "技术栈", "边界", "风险", "互补", "冲突", "权衡"],
            "min_hits": 1,
        }
    )
    cases.append(
        {
            "id": "papers/cross_limits",
            "papers": papers,
            "question": (
                "如果把提供的论文放进同一个技术栈（例如分割骨干、高效视觉骨干、"
                "逆问题求解器等角色），它们各自的适用边界和组合风险是什么？"
                "请引用每篇原文，并给出至少一条“组合使用”时需要警惕的约束。"
            ),
            "concepts": limit_concepts,
            "require_all_docs_cited": True,
            "min_sources_per_doc": 1,
            "min_matrix_slot_ratio": 0.4,
            "max_attribution_issues": 0,
            "max_answer_chars": _max_chars("limits", count),
        }
    )

    cases.append(
        {
            "id": "papers/innovation",
            "kind": "innovation",
            "papers": papers,
            "question": (
                "请从提供的论文中提炼可证伪的创新候选：每个候选给出 "
                "gap（空白/矛盾/局限）、insight、hypothesis、novelty_claim、"
                "prior_art_contrast（相对给定材料的差异）、falsifiable_experiment、"
                "risks 与 confidence，并附原文证据。"
            ),
            "min_candidates": 3,
            "min_supported": 1,
            "min_evidence_docs": min(2, count),
            "max_candidates": 8,
            "max_attribution_issues": 0,
        }
    )

    report_inputs = [f"pdf/{paper['workspace']}" for paper in papers]
    cases.append(
        {
            "id": "papers/report",
            "kind": "report",
            "papers": papers,
            "question": (
                f"请基于提供的 {count} 篇论文撰写一份中文综述报告，"
                f"必须包含以下章节：{'、'.join(REPORT_SECTIONS)}。"
                "要求：比较各篇的核心方法；总结关键实验数字；指出每篇的局限；"
                "给出有证据支撑的创新方向。每个关键结论都要附 "
                "[来源: 文件#锚点]，并且每篇论文都至少引用一次。"
            ),
            "required_sections": list(REPORT_SECTIONS),
            "required_cited_docs": report_inputs,
            "require_all_docs_cited": True,
            "min_sources_per_doc": 1,
            "min_matrix_slot_ratio": 0.4,
            "max_attribution_issues": 0,
            "min_report_chars": 600,
            "max_report_chars": min(24_000, 4000 * count),
            "min_quote_verified_ratio": 0.5,
        }
    )
    return cases


def cases_by_id(
    papers: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    return {case["id"]: case for case in build_cases(
        discover_papers() if papers is None else papers
    )}


def case_inputs(case: dict[str, Any]) -> list[str]:
    return [f"pdf/{paper['workspace']}" for paper in case["papers"]]


def _refs(text: str) -> set[tuple[str, str]]:
    return {
        (path.strip(), anchor.strip())
        for path, anchor in _REF_RE.findall(str(text or ""))
        if path.strip() and anchor.strip()
    }


_PAGE_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def _coverage_pages(coverage: dict[str, Any]) -> dict[str, set[int]]:
    """把 coverage.read_ranges 展开成每份文档已读页码。"""
    result: dict[str, set[int]] = {}
    for document in coverage.get("documents") or []:
        if not isinstance(document, dict):
            continue
        path = str(document.get("path", ""))
        total = max(0, int(document.get("pages") or 0))
        pages: set[int] = set()
        for item in document.get("read_ranges") or []:
            text = str(item).strip().lower()
            if text in {"all", "1-all"}:
                pages.update(range(1, total + 1))
                continue
            match = _PAGE_RANGE_RE.match(text)
            if not match:
                continue
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if start > end:
                start, end = end, start
            pages.update(range(start, end + 1))
        result[path] = pages
    return result


def _soft_citation_issue(
    issue: str, inputs: list[str], coverage_pages: dict[str, set[int]]
) -> bool:
    """页码落在已读范围内、只是没被抽成证据点的引用。"""
    path, _, anchor = str(issue).partition("#")
    if path not in inputs:
        return False
    match = re.search(r"page\s*=\s*(\d+)", anchor)
    return bool(
        match and int(match.group(1)) in coverage_pages.get(path, set())
    )


def _concept_report(
    answer: str, concepts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    report: list[dict[str, Any]] = []
    reasons: list[str] = []
    for group in concepts:
        hits = [
            pattern
            for pattern in group.get("any") or []
            if re.search(pattern, answer, re.IGNORECASE)
        ]
        need = max(1, int(group.get("min_hits", 1)))
        passed = len(hits) >= need
        report.append(
            {
                "name": str(group.get("name", "")),
                "hits": hits,
                "need": need,
                "passed": passed,
            }
        )
        if not passed:
            reasons.append(
                f"缺少概念 {group.get('name')}：命中 {len(hits)}/{need}"
            )
    return report, reasons


def evaluate_result(result: Any, case: dict[str, Any]) -> dict[str, Any]:
    """对一次 document worker 结果做跨文档判定（确定性，不调用模型）。"""
    inputs = case_inputs(case)
    answer = str(getattr(result, "summary", "") or "")
    findings = [str(item) for item in getattr(result, "findings", []) or []]
    sources = [
        dict(item)
        for item in getattr(result, "sources", []) or []
        if isinstance(item, dict)
    ]
    coverage = getattr(result, "coverage", {}) or {}
    documents = [
        item for item in coverage.get("documents") or [] if isinstance(item, dict)
    ]
    coverage_by_path = {
        str(item.get("path", "")): item for item in documents
    }

    required = [
        str(path)
        for path in case.get("required_cited_docs") or []
        if str(path)
    ] or inputs
    cited = _refs(answer)
    cited_paths = {path for path, _ in cited}
    cited_expected = [path for path in required if path in cited_paths]
    per_doc = Counter(str(item.get("path", "")) for item in sources)
    per_doc_sources = {path: int(per_doc.get(path, 0)) for path in inputs}

    matrix = [
        dict(item)
        for item in getattr(result, "matrix", []) or []
        if isinstance(item, dict)
    ]
    matrix_by_path = {
        str(item.get("path", "")): item for item in matrix
    }
    filled_slots = sum(
        1
        for path in inputs
        for key in MATRIX_SLOT_KEYS
        if str(matrix_by_path.get(path, {}).get(key, "")).strip()
    )
    matrix_slot_ratio = (
        filled_slots / (len(inputs) * len(MATRIX_SLOT_KEYS))
        if inputs
        else 0.0
    )
    attribution_issues = [
        dict(item)
        for item in getattr(result, "attribution_issues", []) or []
        if isinstance(item, dict)
    ]
    if not attribution_issues:
        from ubismart.knowledge.attribution import find_attribution_issues

        papers_meta = [
            {
                "path": str(item.get("path", "")),
                "title": str(item.get("title", "")),
            }
            for item in matrix
        ] or [{"path": path} for path in inputs]
        attribution_issues = find_attribution_issues(
            answer, papers_meta
        )

    reasons: list[str] = []
    coverage_complete = len(documents) == len(inputs) and all(
        str(coverage_by_path.get(path, {}).get("status", "")) == "complete"
        for path in inputs
    )
    if not coverage_complete:
        reasons.append(
            "覆盖不完整："
            + json.dumps(
                {
                    path: coverage_by_path.get(path, {}).get("status", "missing")
                    for path in inputs
                },
                ensure_ascii=False,
            )
        )

    if case.get("require_all_docs_cited") and len(cited_expected) < len(required):
        missing = [path for path in required if path not in cited_expected]
        reasons.append("答案未引用要求的文档：" + "、".join(missing))

    min_sources = int(case.get("min_sources_per_doc", 1))
    for path in required:
        if per_doc_sources[path] < min_sources:
            reasons.append(
                f"{path} 证据不足：{per_doc_sources[path]}/{min_sources}"
            )

    min_matrix = float(case.get("min_matrix_slot_ratio", 0.0) or 0.0)
    if min_matrix and matrix_slot_ratio < min_matrix:
        reasons.append(
            "证据矩阵槽位不足："
            f"{matrix_slot_ratio:.0%} < {min_matrix:.0%}"
        )
    max_attr = case.get("max_attribution_issues")
    if max_attr is not None and len(attribution_issues) > int(max_attr):
        reasons.append(
            f"归属错配：{len(attribution_issues)} > {int(max_attr)}"
        )

    max_chars = int(case.get("max_answer_chars", 0) or 0)
    if max_chars and len(answer) > max_chars:
        reasons.append(f"答案过长：{len(answer)} > {max_chars} 字")

    verified = sum(
        1 for source in sources if source.get("quote_verified") is True
    )
    quote_verified_ratio = (
        verified / len(sources) if sources else 0.0
    )
    min_verified = float(case.get("min_quote_verified_ratio", 0.0) or 0.0)
    if min_verified and quote_verified_ratio < min_verified:
        reasons.append(
            "原文跨度校验不足："
            f"{quote_verified_ratio:.0%} < {min_verified:.0%}"
        )

    concept_report, concept_reasons = _concept_report(
        answer, list(case.get("concepts") or [])
    )
    reasons.extend(concept_reasons)

    judge_text = answer + "\n" + "\n".join(findings)
    judgement: dict[str, Any] = {}
    try:
        from ubismart.knowledge.judge import deterministic_judgement

        judgement = deterministic_judgement(
            judge_text, sources, pass_score=60
        )
    except Exception as exc:  # noqa: BLE001 - judge 失败不影响过程指标
        judgement = {"error": f"{type(exc).__name__}: {exc}"}
    citation_warnings: list[str] = []
    if judgement and not judgement.get("supported", False):
        issues = [
            str(item) for item in judgement.get("citation_issues") or []
        ]
        coverage_pages = _coverage_pages(coverage)
        citation_warnings = [
            issue
            for issue in issues
            if _soft_citation_issue(issue, inputs, coverage_pages)
        ]
        hard_issues = [
            issue for issue in issues if issue not in citation_warnings
        ]
        score = int(judgement.get("score") or 0)
        pass_score = int(judgement.get("pass_score") or 60)
        if hard_issues:
            reasons.append(
                "judge 未通过："
                f"score={score} issues={hard_issues}"
            )
        elif score < pass_score:
            reasons.append(
                "judge 分数不足："
                f"{score} < {pass_score}"
            )

    return {
        "case": case["id"],
        "status": str(getattr(result, "status", "") or ""),
        "coverage_complete": coverage_complete,
        "coverage": coverage,
        "citations": len(sources),
        "matrix_entries": len(matrix),
        "matrix_slot_ratio": round(matrix_slot_ratio, 4),
        "attribution_issues": attribution_issues[:10],
        "quote_verified_ratio": round(quote_verified_ratio, 4),
        "unverified_quotes": judgement.get("unverified_quotes", []),
        "required_docs": required,
        "cited_expected_docs": cited_expected,
        "per_doc_sources": per_doc_sources,
        "sources_by_path": dict(Counter(cited_paths)),
        "concepts": concept_report,
        "answer_chars": len(answer),
        "input_tokens": int(getattr(result, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(result, "output_tokens", 0) or 0),
        "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
        "judgement": judgement,
        "citation_warnings": citation_warnings,
        "passed": not reasons,
        "reasons": reasons,
        "answer": answer,
        "findings": findings,
    }


def _has_section(text: str, name: str) -> bool:
    return bool(
        re.search(
            rf"(?im)^\s*#{{1,6}}\s*{re.escape(name)}(?:\s|$)", text
        )
    ) or bool(re.search(rf"(?m)^\s*{re.escape(name)}\s*$", text))


def evaluate_report_case(
    case: dict[str, Any],
    document_result: Any,
    author_result: Any,
    review_result: Any,
    report_text: str,
) -> dict[str, Any]:
    """报告链路判定：文档覆盖 + 报告章节 + 全文档引用 + report_check。"""
    from ubismart.orchestrator.contracts import WorkerResult

    doc_case = dict(case)
    doc_case.update(
        {
            "id": case["id"] + "/doc",
            "concepts": [],
            "max_answer_chars": 0,
            "require_all_docs_cited": False,
            "min_quote_verified_ratio": 0.0,
        }
    )
    document_metrics = evaluate_result(document_result, doc_case)

    reasons: list[str] = []
    if not document_metrics["coverage_complete"]:
        reasons.append("文档覆盖不完整")
    for path, count in document_metrics["per_doc_sources"].items():
        if count < int(case.get("min_sources_per_doc", 1)):
            reasons.append(f"{path} 证据不足：{count}")

    author_status = str(getattr(author_result, "status", ""))
    if author_status != "success":
        reasons.append(
            "author 未成功："
            f"{author_status} {getattr(author_result, 'error', '')}"
        )

    review_findings = [
        str(item) for item in getattr(review_result, "findings", []) or []
    ]
    review_status = str(getattr(review_result, "status", ""))
    if review_status != "success" or not any(
        "校验通过" in item for item in review_findings
    ):
        reasons.append(
            "report_check 未通过："
            f"{review_status} {'；'.join(review_findings[:3])}"
        )

    missing_sections = [
        name
        for name in case.get("required_sections") or []
        if not _has_section(report_text, str(name))
    ]
    if missing_sections:
        reasons.append("缺少章节：" + "、".join(missing_sections))

    min_chars = int(case.get("min_report_chars", 0) or 0)
    if min_chars and len(report_text) < min_chars:
        reasons.append(f"报告过短：{len(report_text)} < {min_chars} 字")

    report_case = dict(case)
    report_case.update({"concepts": [], "max_answer_chars": int(
        case.get("max_report_chars", 0) or 0
    )})
    proxy = WorkerResult(
        task_id=case["id"],
        role="author",
        status="success",
        summary=report_text,
        sources=list(getattr(document_result, "sources", []) or []),
        coverage=getattr(document_result, "coverage", {}) or {},
        matrix=list(getattr(document_result, "matrix", []) or []),
        attribution_issues=[],
    )
    report_metrics = evaluate_result(proxy, report_case)
    reasons.extend(report_metrics["reasons"])

    from ubismart.knowledge.attribution import find_attribution_issues

    matrix = [
        dict(item)
        for item in getattr(document_result, "matrix", []) or []
        if isinstance(item, dict)
    ]
    attribution_issues = find_attribution_issues(
        report_text,
        [
            {
                "path": str(item.get("path", "")),
                "title": str(item.get("title", "")),
            }
            for item in matrix
        ]
        or [{"path": path} for path in case.get("required_cited_docs") or []],
    )
    max_attr = case.get("max_attribution_issues")
    if max_attr is not None and len(attribution_issues) > int(max_attr):
        reasons.append(
            f"报告归属错配：{len(attribution_issues)} > {int(max_attr)}"
        )

    return {
        "case": case["id"],
        "status": author_status,
        "coverage_complete": document_metrics["coverage_complete"],
        "citations": document_metrics["citations"],
        "matrix_entries": document_metrics["matrix_entries"],
        "matrix_slot_ratio": document_metrics["matrix_slot_ratio"],
        "attribution_issues": attribution_issues[:10],
        "quote_verified_ratio": document_metrics["quote_verified_ratio"],
        "required_docs": report_metrics["required_docs"],
        "cited_expected_docs": report_metrics["cited_expected_docs"],
        "per_doc_sources": document_metrics["per_doc_sources"],
        "sections": {
            str(name): _has_section(report_text, str(name))
            for name in case.get("required_sections") or []
        },
        "report_chars": len(report_text),
        "report_check": review_findings,
        "judgement": report_metrics["judgement"],
        "citation_warnings": report_metrics["citation_warnings"],
        "input_tokens": (
            int(getattr(document_result, "input_tokens", 0) or 0)
            + int(getattr(author_result, "input_tokens", 0) or 0)
            + int(getattr(review_result, "input_tokens", 0) or 0)
        ),
        "output_tokens": (
            int(getattr(document_result, "output_tokens", 0) or 0)
            + int(getattr(author_result, "output_tokens", 0) or 0)
            + int(getattr(review_result, "output_tokens", 0) or 0)
        ),
        "duration_ms": (
            int(getattr(document_result, "duration_ms", 0) or 0)
            + int(getattr(author_result, "duration_ms", 0) or 0)
            + int(getattr(review_result, "duration_ms", 0) or 0)
        ),
        "passed": not reasons,
        "reasons": reasons,
        "report_text": report_text,
        "document_metrics": document_metrics,
        "author": {
            "status": author_status,
            "findings": [
                str(item)
                for item in getattr(author_result, "findings", []) or []
            ],
            "output_path": str(
                getattr(author_result, "output_path", "") or ""
            ),
        },
        "review": {
            "status": review_status,
            "findings": review_findings,
        },
    }


def evaluate_innovation_case(
    case: dict[str, Any],
    document_result: Any,
    innovation: dict[str, Any],
) -> dict[str, Any]:
    inputs = case_inputs(case)
    candidates = [
        dict(item)
        for item in innovation.get("candidates") or []
        if isinstance(item, dict)
    ]
    supported = [item for item in candidates if item.get("supported")]
    evidence_docs = {
        path
        for item in candidates
        for path in item.get("evidence_docs") or []
        if path in inputs
    }
    invalid_evidence = [
        (item.get("path"), item.get("anchor"))
        for candidate in candidates
        for item in candidate.get("evidence") or []
        if str(item.get("path", "")) not in inputs
    ]

    coverage = getattr(document_result, "coverage", {}) or {}
    documents = [
        item for item in coverage.get("documents") or [] if isinstance(item, dict)
    ]
    coverage_by_path = {
        str(item.get("path", "")): item for item in documents
    }
    coverage_complete = len(documents) == len(inputs) and all(
        str(coverage_by_path.get(path, {}).get("status", "")) == "complete"
        for path in inputs
    )

    reasons: list[str] = []
    if not coverage_complete:
        reasons.append("文档覆盖不完整")
    if len(candidates) < int(case.get("min_candidates", 1)):
        reasons.append(
            f"创新候选不足：{len(candidates)}/{case.get('min_candidates')}"
        )
    if len(candidates) > int(case.get("max_candidates", 8)):
        reasons.append(
            f"创新候选过多：{len(candidates)}/{case.get('max_candidates')}"
        )
    if len(supported) < int(case.get("min_supported", 1)):
        reasons.append(
            f"通过 critic 的候选不足：{len(supported)}/"
            f"{case.get('min_supported')}"
        )
    if len(evidence_docs) < int(case.get("min_evidence_docs", 1)):
        reasons.append(
            f"证据覆盖文档不足：{len(evidence_docs)}/"
            f"{case.get('min_evidence_docs')}"
        )
    if invalid_evidence:
        reasons.append(f"存在无法归属的证据：{invalid_evidence[:5]}")
    for issue in innovation.get("issues") or []:
        if "没有可归属" in str(issue) or "无法解析" in str(issue):
            reasons.append(str(issue))

    matrix = [
        dict(item)
        for item in getattr(document_result, "matrix", []) or []
        if isinstance(item, dict)
    ]
    from ubismart.knowledge.attribution import find_attribution_issues

    attribution_issues = find_attribution_issues(
        " ".join(
            str(item.get(key, ""))
            for item in candidates
            for key in (
                "title",
                "gap",
                "insight",
                "novelty_claim",
                "prior_art_contrast",
            )
        ),
        [
            {
                "path": str(item.get("path", "")),
                "title": str(item.get("title", "")),
            }
            for item in matrix
        ]
        or [{"path": path} for path in inputs],
    )
    max_attr = case.get("max_attribution_issues")
    if max_attr is not None and len(attribution_issues) > int(max_attr):
        reasons.append(
            f"创新点归属错配：{len(attribution_issues)} > {int(max_attr)}"
        )

    return {
        "case": case["id"],
        "status": "success" if candidates else "partial",
        "coverage_complete": coverage_complete,
        "candidates": len(candidates),
        "supported_candidates": len(supported),
        "evidence_docs": sorted(evidence_docs),
        "attribution_issues": attribution_issues[:10],
        "innovation_issues": list(innovation.get("issues") or [])[:10],
        "input_tokens": int(getattr(document_result, "input_tokens", 0) or 0)
        + int(innovation.get("input_tokens") or 0),
        "output_tokens": int(getattr(document_result, "output_tokens", 0) or 0)
        + int(innovation.get("output_tokens") or 0),
        "duration_ms": int(getattr(document_result, "duration_ms", 0) or 0),
        "passed": not reasons,
        "reasons": reasons,
        "candidates_detail": candidates,
    }


def _prepare_workspace(case: dict[str, Any], base: Path) -> Path:
    root = base / "ws"
    target = root / "pdf"
    target.mkdir(parents=True, exist_ok=True)
    for paper in case["papers"]:
        source = Path(str(paper["source"]))
        if not source.is_file():
            raise FileNotFoundError(f"缺少评测语料：{source}")
        destination = target / str(paper["workspace"])
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    return root


async def _run_case_async(
    case: dict[str, Any],
    *,
    model: str = "",
    judge_llm: bool = False,
    keep_workspace: bool = False,
) -> dict[str, Any]:
    from ubismart.config import get_settings
    from ubismart.knowledge.judge import judge_document_answer
    from ubismart.llm.deepseek import build_llm
    from ubismart.orchestrator.document_workers import make_document_runner
    from ubismart.sandbox.backend import build_sandbox
    from ubismart.tools.registry import build_tools

    # 放在仓库 .agent/ 下，与 pdf/ 同盘，优先硬链接，避免重复拷贝大文件。
    staging = ROOT / ".agent"
    staging.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="papers-", dir=str(staging)))
    try:
        workspace = _prepare_workspace(case, base)
        settings = get_settings().model_copy(
            update={
                "wsl_workspace": str(workspace),
                "document_max_bytes": 40_000_000,
                "document_max_pages": 200,
                "deep_workflows_enabled": True,
                "document_full_coverage": True,
                "document_map_reduce": True,
                "document_map_concurrency": 3,
                "document_matrix_enabled": True,
                "document_tree_fanout": 5,
                "document_tree_input_chars": 16_000,
            }
        )
        sandbox = build_sandbox(settings)
        tools = build_tools(settings, sandbox=sandbox, allow_write=False)
        llm = build_llm(settings, streaming=False, model_override=model)
        runner = make_document_runner(llm=llm, tools=tools, settings=settings)
        started = time.perf_counter()
        result = await runner(
            {
                "id": case["id"].replace("/", "-"),
                "role": "document",
                "goal": case["question"],
                "inputs": case_inputs(case),
                "knowledge_run_id": f"papers-{int(time.time())}",
            },
            parent_request=case["question"],
        )
        report = evaluate_result(result, case)
        report["wall_ms"] = int((time.perf_counter() - started) * 1000)
        if judge_llm:
            report["llm_judgement"] = await judge_document_answer(
                llm,
                case["question"],
                report["answer"] + "\n" + "\n".join(report["findings"]),
                list(result.sources),
                pass_score=int(settings.document_judge_pass_score),
            )
        if keep_workspace:
            report["workspace"] = str(workspace)
        return report
    finally:
        if not keep_workspace:
            shutil.rmtree(base, ignore_errors=True)


async def _run_report_case_async(
    case: dict[str, Any],
    *,
    model: str = "",
    judge_llm: bool = False,
    keep_workspace: bool = False,
) -> dict[str, Any]:
    from ubismart.config import get_settings
    from ubismart.knowledge.judge import judge_document_answer
    from ubismart.llm.deepseek import build_llm
    from ubismart.orchestrator.document_workers import (
        make_author_runner,
        make_document_runner,
        make_report_review_runner,
    )
    from ubismart.sandbox.backend import build_sandbox
    from ubismart.sandbox.fs import SandboxFs
    from ubismart.sandbox.wsl_exec import resolve_workspace
    from ubismart.tools.registry import build_tools

    staging = ROOT / ".agent"
    staging.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="papers-report-", dir=str(staging)))
    try:
        workspace = _prepare_workspace(case, base)
        settings = get_settings().model_copy(
            update={
                "wsl_workspace": str(workspace),
                "document_max_bytes": 40_000_000,
                "document_max_pages": 200,
                "deep_workflows_enabled": True,
                "document_full_coverage": True,
                "document_map_reduce": True,
                "document_map_concurrency": 3,
                "document_matrix_enabled": True,
                "document_tree_fanout": 5,
                "document_tree_input_chars": 16_000,
                "report_output_dir": "reports",
            }
        )
        sandbox = build_sandbox(settings)
        tools = build_tools(
            settings,
            sandbox=sandbox,
            allow_write=True,
            write_scope=["reports"],
        )
        llm = build_llm(settings, streaming=False, model_override=model)
        document_runner = make_document_runner(
            llm=llm, tools=tools, settings=settings
        )
        author_runner = make_author_runner(
            llm=llm, tools=tools, settings=settings
        )
        review_runner = make_report_review_runner(
            tools=tools, llm=llm, settings=settings
        )
        started = time.perf_counter()
        run_id = f"papers-report-{int(time.time())}"
        document_result = await document_runner(
            {
                "id": case["id"].replace("/", "-") + "-doc",
                "role": "document",
                "goal": case["question"],
                "inputs": case_inputs(case),
                "knowledge_run_id": run_id,
            },
            parent_request=case["question"],
        )
        context = {"tasks": [document_result.model_dump()]}
        report_path = "reports/paper-report.md"
        author_result = await author_runner(
            {
                "id": "author",
                "role": "author",
                "goal": case["question"],
                "output_format": "md",
                "output_path": report_path,
                "write_scope": ["reports"],
                "required_sections": list(case.get("required_sections") or []),
                "knowledge_run_id": run_id,
            },
            parent_request=case["question"],
            dependency_context=context,
        )
        review_result = await review_runner(
            {
                "id": "review",
                "role": "review",
                "goal": "校验综述报告的章节与引用",
                "inputs": [report_path],
                "required_sections": list(case.get("required_sections") or []),
            },
            parent_request=case["question"],
            dependency_context=context,
        )
        report_text = ""
        read_error = ""
        try:
            fs = SandboxFs(sandbox, resolve_workspace(settings, sandbox))
            report_text = await asyncio.to_thread(
                fs.read_text, report_path, max_bytes=2_000_000
            )
        except Exception as exc:  # noqa: BLE001 - 读取失败由判定报出
            read_error = f"{type(exc).__name__}: {exc}"
        report = evaluate_report_case(
            case, document_result, author_result, review_result, report_text
        )
        if read_error:
            report["reasons"].append("报告读取失败：" + read_error)
            report["passed"] = False
        report["read_error"] = read_error
        report["wall_ms"] = int((time.perf_counter() - started) * 1000)
        if judge_llm:
            report["llm_judgement"] = await judge_document_answer(
                llm,
                case["question"],
                report_text,
                list(document_result.sources),
                pass_score=int(settings.document_judge_pass_score),
            )
        if keep_workspace:
            report["workspace"] = str(workspace)
        return report
    finally:
        if not keep_workspace:
            shutil.rmtree(base, ignore_errors=True)


async def _run_innovation_case_async(
    case: dict[str, Any],
    *,
    model: str = "",
    judge_llm: bool = False,
    keep_workspace: bool = False,
) -> dict[str, Any]:
    from ubismart.config import get_settings
    from ubismart.knowledge.innovation import extract_innovations
    from ubismart.llm.deepseek import build_llm
    from ubismart.orchestrator.document_workers import make_document_runner
    from ubismart.sandbox.backend import build_sandbox
    from ubismart.tools.registry import build_tools

    staging = ROOT / ".agent"
    staging.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="papers-innovation-", dir=str(staging)))
    try:
        workspace = _prepare_workspace(case, base)
        settings = get_settings().model_copy(
            update={
                "wsl_workspace": str(workspace),
                "document_max_bytes": 40_000_000,
                "document_max_pages": 200,
                "deep_workflows_enabled": True,
                "document_full_coverage": True,
                "document_map_reduce": True,
                "document_map_concurrency": 3,
                "document_matrix_enabled": True,
                "document_tree_fanout": 5,
                "document_tree_input_chars": 16_000,
            }
        )
        sandbox = build_sandbox(settings)
        tools = build_tools(settings, sandbox=sandbox, allow_write=False)
        llm = build_llm(settings, streaming=False, model_override=model)
        document_runner = make_document_runner(
            llm=llm, tools=tools, settings=settings
        )
        started = time.perf_counter()
        document_result = await document_runner(
            {
                "id": case["id"].replace("/", "-"),
                "role": "document",
                "goal": case["question"],
                "inputs": case_inputs(case),
                "knowledge_run_id": f"innovation-{int(time.time())}",
            },
            parent_request=case["question"],
        )
        innovation = await extract_innovations(
            llm,
            document_result.matrix,
            document_result.sources,
            top_k=int(case.get("max_candidates", 8)),
            min_docs=int(case.get("min_evidence_docs", 2)),
        )
        report = evaluate_innovation_case(
            case, document_result, innovation
        )
        report["wall_ms"] = int((time.perf_counter() - started) * 1000)
        if keep_workspace:
            report["workspace"] = str(workspace)
        return report
    finally:
        if not keep_workspace:
            shutil.rmtree(base, ignore_errors=True)


def run_case(
    case: dict[str, Any],
    *,
    model: str = "",
    judge_llm: bool = False,
    keep_workspace: bool = False,
) -> dict[str, Any]:
    runners = {
        "report": _run_report_case_async,
        "innovation": _run_innovation_case_async,
    }
    runner = runners.get(str(case.get("kind") or ""), _run_case_async)
    return asyncio.run(
        runner(
            case,
            model=model,
            judge_llm=judge_llm,
            keep_workspace=keep_workspace,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="列出用例")
    parser.add_argument(
        "--pdf-dir",
        default=str(PDF_DIR),
        help="PDF 目录（默认仓库根目录 pdf/）",
    )
    parser.add_argument(
        "--case", action="append", default=[], help="用例 id，可重复"
    )
    parser.add_argument("--all", action="store_true", help="跑全部用例")
    parser.add_argument("--model", default="", help="覆盖模型名")
    parser.add_argument("--judge-llm", action="store_true", help="额外跑 LLM judge")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--keep-workspace", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    papers = discover_papers(args.pdf_dir)
    cases = build_cases(papers)
    if not cases:
        print(f"目录中没有 PDF：{args.pdf_dir}", file=sys.stderr)
        return 2
    if args.list:
        print(f"发现 {len(papers)} 篇论文：")
        for paper in papers:
            print(f"  - {paper['id']}: {paper['source']}")
        for case in cases:
            print(f"{case['id']}\t{case['question'][:60]}")
        return 0
    selected = (
        cases
        if args.all
        else [case for case in cases if case["id"] in set(args.case)]
    )
    if not selected:
        print("请用 --case 或 --all 指定用例", file=sys.stderr)
        return 2
    reports = [
        run_case(
            case,
            model=args.model,
            judge_llm=args.judge_llm,
            keep_workspace=args.keep_workspace,
        )
        for case in selected
    ]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            print(
                f"{report['case']}: passed={report['passed']} "
                f"coverage={report['coverage_complete']} "
                f"citations={report['citations']} "
                f"tokens={report['input_tokens']}/{report['output_tokens']} "
                f"ms={report['duration_ms']}"
            )
            for reason in report["reasons"]:
                print(f"  - {reason}")
    return 0 if all(report["passed"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
