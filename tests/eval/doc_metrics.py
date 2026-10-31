"""长文档效果指标（无模型依赖的确定性部分）。

与代码评测不同，文档任务没有“项目自带的测试”可判定。这里先提供可复现的
过程指标：覆盖了多少页、引用是否带锚点/原文、claim 是否有证据支撑。
答案正确性仍需要人工或 LLM judge，这里不假装能替代。
"""

from __future__ import annotations

import re
from typing import Any

_RANGE = re.compile(r"^(\d+)(?:-(\d+))?$")


def _expand(range_text: str, pages: int) -> tuple[int, int] | None:
    text = str(range_text or "").strip().lower()
    if text in {"all", "1-all"}:
        return (1, max(1, pages))
    match = _RANGE.match(text)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start > end:
        start, end = end, start
    return (start, end)


def coverage_metrics(coverage: dict[str, Any] | None) -> dict[str, Any]:
    documents = (coverage or {}).get("documents") or []
    total_pages = 0
    read_pages = 0
    unread_ranges: list[str] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        pages = max(0, int(document.get("pages") or 0))
        total_pages += pages
        for item in document.get("read_ranges") or []:
            span = _expand(str(item), pages)
            if span:
                read_pages += span[1] - span[0] + 1
        unread_ranges.extend(
            str(item) for item in document.get("unread") or []
        )
    read_pages = min(read_pages, total_pages) if total_pages else read_pages
    ratio = (read_pages / total_pages) if total_pages else 1.0
    return {
        "status": str((coverage or {}).get("status") or "unknown"),
        "total_pages": total_pages,
        "read_pages": read_pages,
        "coverage_ratio": round(ratio, 4),
        "unread_ranges": unread_ranges[:20],
    }


def citation_metrics(sources: list[dict[str, Any]] | None) -> dict[str, Any]:
    items = [item for item in (sources or []) if isinstance(item, dict)]
    total = len(items)
    if not total:
        return {
            "citations": 0,
            "with_path": 0,
            "with_anchor": 0,
            "with_quote": 0,
            "with_quote_verified": 0,
            "anchor_ratio": 0.0,
            "quote_ratio": 0.0,
            "quote_verified_ratio": 0.0,
        }
    with_path = sum(1 for item in items if str(item.get("path", "")).strip())
    with_anchor = sum(
        1 for item in items if str(item.get("anchor", "")).strip()
    )
    with_quote = sum(
        1 for item in items if str(item.get("quote", "")).strip()
    )
    with_verified = sum(
        1
        for item in items
        if str(item.get("quote", "")).strip()
        and item.get("quote_verified") is not False
    )
    return {
        "citations": total,
        "with_path": with_path,
        "with_anchor": with_anchor,
        "with_quote": with_quote,
        "with_quote_verified": with_verified,
        "anchor_ratio": round(with_anchor / total, 4),
        "quote_ratio": round(with_quote / total, 4),
        "quote_verified_ratio": round(with_verified / total, 4),
    }


def evidence_coverage(
    claims: list[dict[str, Any]] | None,
    citations: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    claim_items = [item for item in (claims or []) if isinstance(item, dict)]
    if not claim_items:
        return {"claims": 0, "supported": 0, "coverage_ratio": 0.0}
    keys = {
        (
            str(item.get("path", "")).strip(),
            str(item.get("anchor", "")).strip(),
        )
        for item in (citations or [])
        if isinstance(item, dict)
    }
    supported = 0
    for claim in claim_items:
        key = (
            str(claim.get("evidence_path", "")).strip(),
            str(claim.get("anchor", "")).strip(),
        )
        if key in keys and key != ("", ""):
            supported += 1
    return {
        "claims": len(claim_items),
        "supported": supported,
        "coverage_ratio": round(supported / len(claim_items), 4),
    }


def summarize_document_run(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(result.get("task_id", "")),
        "role": str(result.get("role", "")),
        "status": str(result.get("status", "")),
        "coverage": coverage_metrics(result.get("coverage") or {}),
        "citations": citation_metrics(result.get("sources") or []),
        "findings": len(result.get("findings") or []),
        "input_tokens": int(result.get("input_tokens") or 0),
        "output_tokens": int(result.get("output_tokens") or 0),
    }
