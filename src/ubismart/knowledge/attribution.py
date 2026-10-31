"""跨文档归属校验：句子提到哪篇，就应引用哪篇。

只做确定性检查，不判断语义；用于把“DiffFNO 的结论引到 paper-6”这类
错配从报告里暴露出来。别名来自文档路径 stem 与矩阵标题。
"""

from __future__ import annotations

import re
from typing import Any

_REF_RE = re.compile(
    r"\[来源\s*[:：]\s*([^\]#]+?)#([^\]]+?)\s*\]"
)
_SEGMENT_RE = re.compile(r"(?<=[。！？!?])\s*|\n+")
_GENERIC_ALIASES = {
    "paper",
    "document",
    "file",
    "report",
    "model",
    "method",
    "diffusion",
    "论文",
    "文档",
}


def normalize_alias(text: str) -> str:
    return re.sub(
        r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").lower()
    )


def _path_stem(path: str) -> str:
    return str(path).rsplit("/", 1)[-1].rsplit(".", 1)[0]


def document_aliases(
    papers: list[dict[str, Any]],
) -> dict[str, str]:
    """alias -> path；歧义别名（指向多篇）会被丢弃。"""
    candidates: dict[str, set[str]] = {}
    for paper in papers:
        path = str(paper.get("path", "")).strip()
        if not path:
            continue
        title = str(paper.get("title", "")).strip()
        values = [
            _path_stem(path),
            title,
            title.split(":", 1)[0] if ":" in title else "",
        ]
        for value in values:
            alias = normalize_alias(value)
            if len(alias) < 3 or alias in _GENERIC_ALIASES:
                continue
            candidates.setdefault(alias, set()).add(path)
    return {
        alias: next(iter(paths))
        for alias, paths in candidates.items()
        if len(paths) == 1
    }


def _mentions(segment_norm: str, aliases: dict[str, str]) -> set[str]:
    found: set[str] = set()
    for alias, path in aliases.items():
        pattern = (
            rf"(?<![0-9a-z]){re.escape(alias)}(?![0-9a-z])"
        )
        if re.search(pattern, segment_norm):
            found.add(path)
    return found


def find_attribution_issues(
    text: str,
    papers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """返回归属错配：提到 A/B 却只引用 C（且 C 不在被提到的集合里）。"""
    aliases = document_aliases(papers)
    if not aliases:
        return []
    issues: list[dict[str, Any]] = []
    for raw in _SEGMENT_RE.split(str(text or "")):
        segment = raw.strip()
        if not segment:
            continue
        cited = {
            path.strip()
            for path, _anchor in _REF_RE.findall(segment)
            if path.strip()
        }
        if not cited:
            continue
        mentioned = _mentions(normalize_alias(segment), aliases)
        if not mentioned:
            continue
        if mentioned & cited:
            continue
        issues.append(
            {
                "mentioned": sorted(mentioned),
                "cited": sorted(cited),
                "text": segment[:240],
            }
        )
    return issues


def attribution_issue_count(
    text: str, papers: list[dict[str, Any]]
) -> int:
    return len(find_attribution_issues(text, papers))
