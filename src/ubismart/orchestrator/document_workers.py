"""文档/图像/报告 worker。

这些 runner 刻意保持“单次结构化调用”而不是完整工具循环：
- `document`：调用 document_read 取带锚点文本，再用一次结构化抽取关键点；
- `vision`：调用 image_inspect（内部已走多模态模型）；
- `author`：基于依赖上下文生成 Markdown，再调用 report_export 落盘。

它们共享 orchestrator 的 scheduler/fan-in/重试，不新增主图节点。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from ubismart.knowledge.attribution import find_attribution_issues
from ubismart.llm.deepseek import with_json_output
from ubismart.orchestrator.contracts import WorkerResult
from ubismart.orchestrator.implement import _within_scope
from ubismart.tools.artifacts import unpack
from ubismart.tools.documents import DOCUMENT_READ, PDF_PAGE_IMAGE
from ubismart.tools.report import (
    REPORT_CHECK,
    REPORT_EXPORT,
    REPORT_READ,
    resolve_output_format,
)
from ubismart.tools.vision import IMAGE_INSPECT

_MAX_SOURCE_CHARS = 36_000
_MAX_FILES = 64


class DocumentPoint(BaseModel):
    claim: str = Field(description="一条关键信息/结论")
    evidence_path: str = Field(default="", description="来源文件相对路径")
    anchor: str = Field(default="", description="页码/工作表/幻灯片/段落锚点")
    quote: str = Field(default="", description="支撑该结论的原文片段")
    confidence: str = Field(default="medium", description="high/medium/low")


class DocumentAnalysis(BaseModel):
    summary: str = Field(description="一段话总结")
    points: list[DocumentPoint] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class SummaryOnly(BaseModel):
    summary: str = ""


_MATRIX_SLOT_HINTS: dict[str, tuple[str, ...]] = {
    "problem": ("问题", "针对", "挑战", "稀缺", "需要", "复杂"),
    "method": (
        "方法",
        "结构",
        "模块",
        "卷积",
        "池化",
        "上采样",
        "跳跃",
        "编码",
        "解码",
        "扫描",
        "SSM",
        "扩散",
        "网络",
    ),
    "algorithm": (
        "算法",
        "公式",
        "Tweedie",
        "采样",
        "梯度",
        "步骤",
        "递推",
    ),
    "experiments": (
        "实验",
        "结果",
        "PSNR",
        "FID",
        "IOU",
        "top-1",
        "吞吐",
        "数据集",
        "指标",
    ),
    "limitations": (
        "局限",
        "不足",
        "未能",
        "未探索",
        "慢",
        "开销",
        "限制",
    ),
    "innovations": ("创新", "提出", "贡献", "首次", "改进"),
    "conclusion": ("结论", "表明", "证明", "说明"),
}

MATRIX_SLOTS: tuple[tuple[str, str], ...] = (
    ("problem", "问题/动机"),
    ("method", "方法"),
    ("algorithm", "算法/公式"),
    ("experiments", "实验/数字"),
    ("limitations", "局限"),
    ("innovations", "创新/可借鉴"),
    ("conclusion", "结论"),
)


class DocumentMatrix(BaseModel):
    path: str = ""
    title: str = ""
    problem: str = ""
    method: str = ""
    algorithm: str = ""
    experiments: str = ""
    limitations: str = ""
    innovations: str = ""
    conclusion: str = ""
    evidence: list[DocumentPoint] = Field(default_factory=list)


class EvidenceReview(BaseModel):
    supported: bool = Field(
        default=True, description="报告结论是否都能被给定证据支持"
    )
    issues: list[str] = Field(default_factory=list)
    summary: str = ""


DOCUMENT_PROMPT = """\
你是文档分析 worker。只根据给出的文档内容提炼关键信息，不要臆造。
每条结论必须带来源路径与锚点（页码/工作表/幻灯片/段落）；找不到锚点时 confidence=low。
summary 用中文书写，关键结论后用 [来源: 文件路径#锚点] 内联标注（至少 3 处），
锚点必须真实出现在给定内容里，禁止编造。
输出 JSON：{"summary": "...", "points": [{"claim": "...", "evidence_path": "...",
"anchor": "...", "quote": "...", "confidence": "high|medium|low"}],
"open_questions": []}
"""

CONSOLIDATE_PROMPT = """\
你是文档分析 worker。下面是一个或多个文档不同片段的中文摘要，每段以
“## 路径”开头。请合并成一段连贯、无重复的中文总结：
- 按文档保留关键定义、数字与结论；**不要因为某篇摘要较短就判定该文档没有正文**；
- 严格按 `## 路径` 归属结论，不要把 A 的方法/数字写到 B 名下；
- 必须覆盖输入中出现的每个文档路径，删除重复表述；
- 每篇文档至少保留 1 条关键结论；summary 总长按文档数扩展，最多 4000 个汉字；
- 最多保留 8 个 [来源: 文件路径#锚点] 引用；只能沿用输入摘要里已有的
  路径与锚点，不要新造 Table/Figure 之类输入中不存在的锚点；
- 只输出 JSON：{"summary": "..."}
"""

MATRIX_PROMPT = """\
你是文档结构化验证据 worker。只依据给定的摘要与证据，输出固定槽位。
- title 用论文标题；无法确定时写空串；
- problem/method/algorithm/experiments/limitations/innovations/conclusion
  各写 1–3 句中文；证据不足的槽位写空串，不要用“未知/无”填充；
- 每个槽位的关键结论附 [来源: 文件路径#锚点]；锚点只能来自给定证据，
  禁止把章节号、公式号、作者名或其它文档的锚点写进来；
- evidence 只允许复制给定证据，不要新增；
- 只输出 JSON：{"path":"...","title":"...","problem":"...","method":"...",
  "algorithm":"...","experiments":"...","limitations":"...","innovations":"...",
  "conclusion":"...","evidence":[{"claim":"...","evidence_path":"...",
  "anchor":"...","quote":"...","confidence":"high|medium|low"}]}
"""

TREE_MERGE_PROMPT = """\
你是多文档综述的归并节点。输入是若干“文档矩阵”或上一层的分组摘要，
每段以“## 路径”开头。请合并为一段高层中文摘要：
- 保留每篇的核心问题、方法、关键数字、局限与创新；
- **严格按路径归属**，绝不能把 A 的方法/数字写到 B 名下；
- 删除重复表述，但不要省略任何输入路径；
- 引用只能沿用输入中已有的 [来源: 路径#锚点]，不要新造锚点；
- 只输出 JSON：{"summary": "..."}
"""

AUTHOR_PROMPT = """\
你是报告撰写 worker。根据依赖任务提供的摘要、发现与引用锚点撰写报告。
要求：
- 用中文；结构清晰（标题、要点、结论、局限性）；
- 若上下文提供了“宿主自动检索到的证据”，优先据此写作；如果
  knowledge_search 工具可用，先用它补充与本章节相关的证据；
- 每条关键结论尽量标注来源，例如 [来源: path#page=3]；
- **引用锚点只能原样使用上游“证据/引用”行里出现的锚点**（如 page=3、
  outline=5、Table 1）；不要用章节号、公式号、作者名或“文档头信息”等
  自造锚点。找不到合适锚点时宁可不写引用，也不要编造；
- 不编造依赖上下文/检索结果里没有的事实；缺失信息写入“局限性”；
- 只输出 Markdown 正文，不要额外说明。
"""


def _strip_document_header(text: str) -> str:
    """去掉 document_read 的元信息头（路径/字节/页数/任务意图）。

    这些头不是论文正文；混进 map 片段会让模型把“共 N 个内容块”当成结论。
    """
    value = str(text or "")
    if not value.startswith("文档 "):
        return value
    _, separator, body = value.partition("\n\n")
    return body if separator else ""


def _find_tool(tools: list[Any], name: str) -> Any | None:
    for tool in tools or []:
        if getattr(tool, "name", "") == name:
            return tool
    return None


def _usage(message: Any) -> tuple[int, int]:
    meta = getattr(message, "usage_metadata", None) or {}
    return (
        int(meta.get("input_tokens") or 0),
        int(meta.get("output_tokens") or 0),
    )


_SECTION_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+\S"
    r"|\d+(?:\.\d+)*[.)]?\s+[A-Za-z\u4e00-\u9fff]"
    r"|(?:abstract|introduction|related work|background|method|methods|"
    r"approach|experiments?|results?|discussion|conclusions?|limitations?|"
    r"appendix|references|acknowledg\w*)\b)",
    re.IGNORECASE,
)
_CAPTION_RE = re.compile(r"^(?:figure|table|algorithm)\s+\d+", re.IGNORECASE)


def _is_section_heading(line: str) -> bool:
    """识别论文/报告里的章节标题，忽略页码前缀与图表标题。"""
    text = re.sub(r"^\[[^\]]+\]\s*", "", str(line or "")).strip()
    if not text or len(text) > 100:
        return False
    if _CAPTION_RE.match(text):
        return False
    if text.endswith((".", "。", "；", ";", ",")):
        return False
    if _SECTION_HEADING_RE.match(text):
        return True
    # 全大写短行通常也是标题（如 "EXPERIMENTS"）。
    return bool(re.match(r"^[A-Z][A-Z0-9 &/\-]{2,60}$", text))


def _split_sections(text: str, limit: int) -> list[str]:
    """优先按章节切分；超长章节再按段落切，并给每片带上标题。"""
    limit = max(500, int(limit))
    raw = str(text or "")
    if not raw:
        return []
    lines = raw.splitlines(keepends=True)
    starts = [
        index for index, line in enumerate(lines) if _is_section_heading(line)
    ]
    if not starts:
        return _split_text(raw, limit)
    if starts[0] != 0:
        starts = [0, *starts]
    starts = sorted(set(starts))

    sections: list[str] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        section = "".join(lines[start:end]).strip()
        if section:
            sections.append(section)

    units: list[str] = []
    for section in sections:
        if len(section) <= limit:
            units.append(section)
            continue
        head, _, body = section.partition("\n")
        head = head.strip()
        body = body.strip()
        if not body:
            units.extend(_split_text(section, limit))
            continue
        room = max(500, limit - len(head) - 1)
        units.extend(
            f"{head}\n{part}" if head else part
            for part in _split_text(body, room)
        )
    return units or _split_text(raw, limit)


def _split_text(text: str, limit: int) -> list[str]:
    """按段落优先切分；超长段落硬切，保证每片不超过 limit。"""
    limit = max(500, int(limit))
    if len(text) <= limit:
        return [text] if text else []
    units: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            if current:
                units.append(current)
                current = ""
            units.append(para[:limit])
            para = para[limit:]
        if current and len(current) + len(para) + 2 > limit:
            units.append(current)
            current = ""
        current += ("\n\n" if current else "") + para
    if current:
        units.append(current)
    return units or [text[:limit]]


def _merge_analyses(
    analyses: list[DocumentAnalysis], *, point_limit: int = 200
) -> DocumentAnalysis:
    """合并分批抽取结果：summary 拼接、points 去重、开放问题合并。"""
    summaries: list[str] = []
    points: list[DocumentPoint] = []
    questions: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for analysis in analyses:
        summary = analysis.summary.strip()
        if summary and summary not in summaries:
            summaries.append(summary)
        for point in analysis.points:
            key = (
                point.claim.strip(),
                point.evidence_path.strip(),
                point.anchor.strip(),
            )
            if not key[0] or key in seen:
                continue
            seen.add(key)
            points.append(point)
        for question in analysis.open_questions:
            text = str(question).strip()
            if text and text not in questions:
                questions.append(text)
    return DocumentAnalysis(
        summary="；".join(summaries[:5]) or "（无摘要）",
        points=points[:point_limit],
        open_questions=questions[:20],
    )


def _append_evidence_refs(
    summary: str, sources: list[dict[str, Any]], limit: int = 10
) -> str:
    """摘要缺少内联引用时，追加真实证据锚点，保证可回指。

    只使用已抽取到的 sources，不新增或改写任何结论；已有引用时不重复。
    """
    refs: list[str] = []
    seen: set[str] = set()
    for item in sources:
        path = str(item.get("path", "")).strip()
        anchor = str(item.get("anchor", "")).strip()
        if not path or not anchor:
            continue
        ref = f"{path}#{anchor}"
        if ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        if len(refs) >= limit:
            break
    if not refs:
        return summary
    missing = [ref for ref in refs if ref not in summary]
    if not missing:
        return summary
    return summary + "\n\n主要证据：" + "；".join(
        f"[来源: {ref}]" for ref in missing
    )


_CITATION_REF_RE = re.compile(
    r"\[来源\s*[:：]\s*([^\]#]+?)#"
    r"((?:[^\[\]]|\[[^\]]*\])*?)\s*\]"
)
_SUMMARY_FIELD_RE = re.compile(
    r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', re.S
)


_PLACEHOLDER_PATHS = {
    "文件",
    "文档",
    "论文",
    "附件",
    "document",
    "file",
    "paper",
    "pdf",
    "attachment",
}


def _is_placeholder_path(path: str) -> bool:
    """判断模型是否只写了“文件/文档”这类占位名，而不是真实路径。"""
    text = str(path or "").strip().lower()
    if not text:
        return True
    if text in _PLACEHOLDER_PATHS:
        return True
    return "." not in text and "/" not in text and "\\" not in text


def _citation_alias(path: str) -> str:
    stem = str(path).rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", stem)


def _normalize_citation_paths(
    summary: str,
    doc_units: list[dict[str, Any]],
    inputs: list[str],
    sources: list[dict[str, Any]] | None = None,
) -> str:
    """把摘要里的占位路径（“文件”“文档”）替换成真实来源。

    多文档时先看引用前文最近提到的论文名（来自文件名，如 DPS/UNet/VMamba）；
    没有可用的名字线索时，再尝试“该锚点只属于一份文档”的唯一匹配。
    无法消歧就保留原样，交给 judge 报出来。
    """
    text = str(summary or "")
    known = {
        str(unit.get("path", "")).strip()
        for unit in doc_units
        if str(unit.get("path", "")).strip()
    }
    known.update(str(path).strip() for path in inputs if str(path).strip())
    if not known:
        return text
    default = next(iter(known)) if len(known) == 1 else ""
    aliases = {
        alias: path
        for path in known
        # 单字母/过短的文件名（a.pdf、b.pdf）容易误匹配，不作为名字线索。
        if (alias := _citation_alias(path)) and len(alias) >= 3
    }
    anchor_paths: dict[str, set[str]] = {}
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        path = str(source.get("path", "")).strip()
        anchor = str(source.get("anchor", "")).strip()
        if path in known and anchor:
            anchor_paths.setdefault(anchor, set()).add(path)
    anchor_aliases: dict[str, str] = {}
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        original = str(source.get("anchor_original", "")).strip()
        current = str(source.get("anchor", "")).strip()
        if original and current and original != current:
            anchor_aliases.setdefault(original, current)

    def _replace(match: re.Match[str]) -> str:
        path, anchor = match.group(1), match.group(2)
        anchor_key = anchor.strip()
        normalized_anchor = anchor_aliases.get(anchor_key, anchor)
        target = ""
        if path in known:
            target = path
        elif _is_placeholder_path(path):
            if default:
                target = default
            else:
                tail = text[max(0, match.start() - 600) : match.start()]
                tail_norm = re.sub(
                    r"[^0-9a-z\u4e00-\u9fff]+", "", tail.lower()
                )
                best_position, best_path = -1, ""
                for alias, candidate in aliases.items():
                    position = tail_norm.rfind(alias)
                    if position > best_position:
                        best_position, best_path = position, candidate
                if best_path:
                    target = best_path
                else:
                    unique = anchor_paths.get(anchor_key) or set()
                    if len(unique) == 1:
                        target = next(iter(unique))
        else:
            return match.group(0)
        if not target:
            return match.group(0)
        if target == path and normalized_anchor == anchor:
            return match.group(0)
        return f"[来源: {target}#{normalized_anchor}]"

    return _CITATION_REF_RE.sub(_replace, text)


def _resolve_evidence_path(
    path: str,
    quote: str,
    doc_units: list[dict[str, Any]],
    inputs: list[str],
) -> str:
    """把模型自造的路径（如“文件”“文档”）纠正回真实来源。

    先按 quote 在已读文本中回查；单文档直接使用唯一路径；多文档无法
    定位时返回空串，宁可少一条引用也不张冠李戴。
    """
    candidate = str(path or "").strip()
    known = {str(unit.get("path", "")) for unit in doc_units if unit.get("path")}
    if candidate in known:
        return candidate
    needle = str(quote or "").strip()[:80]
    if needle:
        for unit in doc_units:
            if needle in str(unit.get("text") or ""):
                return str(unit.get("path", ""))
    # 只纠正“文件/文档”这类占位名；真实路径可能是同批另一份文档，
    # 交给上层/报告自检去核验，避免张冠李戴。
    if _is_placeholder_path(candidate):
        if len(doc_units) == 1:
            return str(doc_units[0].get("path", ""))
        return ""
    return candidate or (inputs[0] if inputs else "")


_PAGE_MARK_RE = re.compile(r"\[page=(\d+)\]")
_ANCHOR_TOKEN_RE = re.compile(
    r"\[((?:page|slide|sheet|outline|table|figure|algorithm|image|"
    r"attachment|line|heading|paragraph|section|part|note|form)"
    r"[^\]]*)\]",
    re.IGNORECASE,
)


def _document_anchors(text: str) -> set[str]:
    return {
        match.group(1).strip()
        for match in _ANCHOR_TOKEN_RE.finditer(str(text or ""))
    }


def _normalize_match(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _quote_index(text: str) -> tuple[str, list[tuple[int | None, int, int]]]:
    """归一化全文，并记录每页在归一化文本中的区间。"""
    normalized = ""
    spans: list[tuple[int | None, int, int]] = []

    def _add(segment: str, page: int | None) -> None:
        nonlocal normalized
        value = _normalize_match(segment)
        if not value:
            return
        if normalized:
            normalized += " "
        start = len(normalized)
        normalized += value
        spans.append((page, start, len(normalized)))

    matches = list(_PAGE_MARK_RE.finditer(text))
    if not matches:
        _add(text, None)
        return normalized, spans
    if matches[0].start() > 0:
        _add(text[: matches[0].start()], None)
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        _add(text[start:end], int(match.group(1)))
    return normalized, spans


def _find_quote_span(
    quote: str,
    normalized: str,
    spans: list[tuple[int | None, int, int]],
) -> tuple[bool, dict[str, int | None] | None]:
    needle = _normalize_match(quote)
    if not needle or not normalized:
        return False, None
    start = normalized.find(needle)
    if start < 0:
        return False, None
    end = start + len(needle)
    page = next(
        (
            page_number
            for page_number, span_start, span_end in spans
            if span_start <= start < span_end
        ),
        None,
    )
    return True, {"start": start, "end": end, "page": page}


def _verify_source_quotes(
    sources: list[dict[str, Any]], doc_units: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把每条证据的 quote 回到原文定位；记录是否命中与字符跨度。"""
    indexes: dict[str, tuple[str, list[tuple[int | None, int, int]]]] = {}
    anchors_by_path: dict[str, set[str]] = {}
    for unit in doc_units:
        path = str(unit.get("path", ""))
        if path:
            text = str(unit.get("text", ""))
            indexes[path] = _quote_index(text)
            anchors_by_path[path] = _document_anchors(text)
    for source in sources:
        path = str(source.get("path", ""))
        quote = str(source.get("quote", ""))
        index = indexes.get(path)
        if index is None:
            source["quote_verified"] = False
            source["anchor_valid"] = False
            continue
        verified, span = _find_quote_span(quote, index[0], index[1])
        source["quote_verified"] = verified
        if span is not None:
            source["quote_span"] = span
            page = span.get("page")
            anchor = str(source.get("anchor", "")).strip()
            # 章节号/公式号/作者名不是文档锚点；原文定位成功时回写成真实页码。
            if page is not None and re.match(
                r"^page=\d+\s*[-–]\s*\d+$", anchor
            ):
                source["anchor_original"] = anchor
                source["anchor"] = f"page={page}"
            elif (
                page is not None
                and (match := re.match(r"^page=(\d+)(\s+.+)$", anchor))
                and not match.group(2).strip().startswith("/")
            ):
                # "page=8 Table 2" 不是解析器锚点；回写成该 quote 的实际页码。
                source["anchor_original"] = anchor
                source["anchor"] = f"page={page}"
            elif page is not None and anchor and not anchor.startswith("page="):
                source["anchor_original"] = anchor
                source["anchor"] = f"page={page}"
            elif page is not None and not anchor:
                source["anchor"] = f"page={page}"
        anchors = anchors_by_path.get(path) or set()
        source["anchor_valid"] = (
            not anchors
            or str(source.get("anchor", "")).strip() in anchors
        )
    return sources


def _balanced_sources(
    sources: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """按文档轮转取证据，避免某一份长文档占满上下文。"""
    limit = max(0, int(limit))
    if limit <= 0:
        return []
    groups: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        path = str(source.get("path", "")).strip()
        groups.setdefault(path, []).append(source)
    if not groups:
        return []
    selected: list[dict[str, Any]] = []
    index = 0
    while len(selected) < limit:
        added = False
        for items in groups.values():
            if index < len(items):
                selected.append(items[index])
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        index += 1
    return selected


_ANCHOR_HINT_RE = re.compile(
    r"^(?:page|slide|sheet|outline|table|figure|algorithm|image|"
    r"attachment|line|heading|paragraph|section|part|note|form)\b",
    re.IGNORECASE,
)


def _is_plausible_anchor(anchor: str) -> bool:
    """文档锚点应能对应到解析器产生的坐标，而不是“无/未知/正文”等描述。"""
    text = str(anchor or "").strip()
    if not text:
        return False
    return bool(_ANCHOR_HINT_RE.match(text) or "=" in text)


def _render_matrix(entry: dict[str, Any]) -> str:
    path = str(entry.get("path", "")).strip()
    title = str(entry.get("title", "")).strip()
    lines = [f"## {path}" + (f" | {title}" if title else "")]
    for key, label in MATRIX_SLOTS:
        value = _strip_citation_markers(
            str(entry.get(key, ""))
        ).strip()
        if value:
            lines.append(f"{label}：{value[:300]}")
    refs: list[str] = []
    for point in entry.get("evidence") or []:
        if not isinstance(point, dict):
            continue
        ref = (
            f"{point.get('evidence_path', '')}"
            f"#{point.get('anchor', '')}"
        )
        if ref.strip("#") and ref not in refs:
            refs.append(ref)
    if refs:
        lines.append("证据：" + "；".join(refs[:5]))
    return "\n".join(lines)


def _render_matrix_compact(entry: dict[str, Any]) -> str:
    """最终答案里的紧凑矩阵行：保证树状归并后每篇槽位仍可见。"""
    path = str(entry.get("path", "")).strip()
    title = str(entry.get("title", "")).strip()
    parts: list[str] = []
    for key, label, limit in (
        ("problem", "问题", 120),
        ("method", "方法", 220),
        ("algorithm", "算法", 120),
        ("experiments", "实验", 180),
        ("limitations", "局限", 120),
        ("innovations", "创新", 120),
    ):
        value = _strip_citation_markers(
            str(entry.get(key, ""))
        ).strip()
        if value:
            parts.append(f"{label}：{value[:limit]}")
    if not parts:
        return ""
    return f"- {path}" + (f"｜{title}" if title else "") + "：" + "；".join(parts)


def _strip_citation_markers(text: str) -> str:
    return re.sub(
        r"\[来源\s*[:：](?:[^\[\]]|\[[^\]]*\])*\]",
        "",
        str(text or ""),
    )


def _sanitize_citations(
    text: str, sources: list[dict[str, Any]]
) -> str:
    """只保留能对应到证据的引用；对应不上的标记直接去掉，不编造新锚点。"""
    known: dict[str, set[str]] = {}
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        path = str(source.get("path", "")).strip()
        anchor = str(source.get("anchor", "")).strip()
        if path and anchor:
            known.setdefault(path, set()).add(anchor)
    if not known:
        return str(text or "")

    def _replace(match: re.Match[str]) -> str:
        path, anchor = match.group(1).strip(), match.group(2).strip()
        anchors = known.get(path)
        if not anchors:
            return ""
        if anchor in anchors:
            return match.group(0)
        candidates = [
            candidate
            for candidate in anchors
            if anchor in candidate or candidate in anchor
        ]
        if len(candidates) == 1:
            return f"[来源: {path}#{candidates[0]}]"
        return ""

    return _CITATION_REF_RE.sub(_replace, str(text or ""))


def _context_text(
    dependency_context: dict[str, Any],
    max_chars: int = 12_000,
    *,
    source_limit: int = 20,
    quote_chars: int = 160,
) -> str:
    """把依赖结果渲染成有界文本。

    每条 summary/finding/source 都有长度上限，整体再受 `max_chars` 约束，
    避免多文档时把上游摘要堆成超过 prompt 预算的巨型上下文。
    """
    budget = max(1_000, int(max_chars))
    lines: list[str] = []
    used = 0
    truncated = False

    def add(line: str) -> bool:
        nonlocal used, truncated
        text = str(line)
        if used + len(text) + 1 > budget:
            truncated = True
            return False
        lines.append(text)
        used += len(text) + 1
        return True

    stopped = False
    for entry in (dependency_context or {}).get("tasks") or []:
        if stopped:
            break
        add(
            f"- {entry.get('task_id')} ({entry.get('role')}/{entry.get('status')}): "
            f"{_strip_citation_markers(str(entry.get('summary', '')))[:1_500]}"
        )
        for item in entry.get("matrix") or []:
            if stopped:
                break
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", ""))
            title = str(item.get("title", ""))
            if not add(
                f"  · 矩阵 {path}" + (f" | {title}" if title else "")
            ):
                stopped = True
                break
            for key, label in MATRIX_SLOTS:
                value = str(item.get(key, "")).strip()
                if value and not add(f"    {label}: {value[:300]}"):
                    stopped = True
                    break
        for finding in (entry.get("findings") or [])[:8]:
            if stopped:
                break
            if not add(f"  · {str(finding)[:500]}"):
                stopped = True
                break
        for source in _balanced_sources(
            list(entry.get("sources") or []), int(source_limit)
        ):
            claim = str(source.get("claim", "")).strip()
            quote = str(source.get("quote", ""))[: max(0, int(quote_chars))]
            label = "证据" if claim else "引用"
            detail = f"{claim} — {quote}" if claim and quote else (claim or quote)
            if not add(
                f"  · {label}："
                f"{source.get('path', '')}#{source.get('anchor', '')} "
                f"{detail}"
            ):
                stopped = True
                break
    if truncated and used + 16 <= budget:
        lines.append("…（依赖上下文已截断）")
    return "\n".join(lines) or "（依赖任务没有可用摘要）"


def make_document_runner(
    *,
    llm: BaseChatModel,
    tools: list[Any],
    settings: Any,
    image_tool: Any | None = None,
    knowledge_store: Any | None = None,
) -> Any:
    structured = with_json_output(llm, DocumentAnalysis, include_raw=True)
    consolidator = with_json_output(llm, SummaryOnly, include_raw=True)
    matrix_structured = with_json_output(
        llm, DocumentMatrix, include_raw=True
    )

    async def run(
        task: dict[str, Any],
        *,
        parent_request: str,
        session_brief: str = "",
        cwd: str = "",
        config: RunnableConfig | None = None,
        cancel: Any = None,
        dependency_context: dict[str, Any] | None = None,
        memories: list[str] | None = None,
        instructions: str = "",
    ) -> WorkerResult:
        started = time.perf_counter()
        task_id = str(task.get("id", ""))
        goal = str(task.get("goal", ""))
        inputs = [str(item) for item in (task.get("inputs") or [])][:_MAX_FILES]
        if cancel is not None and cancel():
            return WorkerResult(
                task_id=task_id, role="document", worker_id=f"document:{task_id}",
                goal=goal, status="cancelled", summary="已取消",
            )
        tool = _find_tool(tools, DOCUMENT_READ)
        chunks: list[str] = []
        warnings: list[str] = []
        if inputs and tool is None:
            return WorkerResult(
                task_id=task_id, role="document", worker_id=f"document:{task_id}",
                goal=goal, status="failed", summary="document_read 工具未配置",
            )
        page_size = max(
            1, int(getattr(settings, "document_read_pages_per_call", 5))
        )
        max_calls = max(
            1, int(getattr(settings, "document_read_max_calls", 3))
        )

        page_tool = _find_tool(tools, PDF_PAGE_IMAGE)
        # RFC-0009：默认关闭；开启后取消固定 3 次读取，并允许分批抽取。
        deep = bool(getattr(settings, "deep_workflows_enabled", False))
        full_coverage = bool(
            deep and getattr(settings, "document_full_coverage", False)
        )
        map_reduce = bool(
            deep and getattr(settings, "document_map_reduce", False)
        )
        page_cap_limit = max(
            1, int(getattr(settings, "document_max_pages", 200) or 1)
        )
        map_chunk_chars = max(
            500, int(getattr(settings, "document_chunk_chars", 6_000) or 6_000)
        )
        map_concurrency = max(
            1,
            int(
                getattr(settings, "document_map_concurrency", 3) or 1
            ),
        )
        # 非 PDF 没有页范围可分批，靠一次读取上限 + map-reduce 切分。
        # 默认 12k 只适合短文档；deep 模式提高到 200k，再由 map 切分。
        non_pdf_max_chars = 12_000
        if map_reduce:
            non_pdf_max_chars = min(
                200_000,
                max(
                    12_000,
                    int(
                        getattr(settings, "document_max_chars", 2_000_000)
                        or 2_000_000
                    ),
                ),
            )
        coverage_docs: list[dict[str, Any]] = []
        doc_units: list[dict[str, Any]] = []

        async def _read(
            path: str, **kwargs: Any
        ) -> tuple[str, dict[str, Any] | None]:
            raw = await asyncio.to_thread(
                tool.invoke,
                {"path": path, "reason": goal, **kwargs},
            )
            text, artifact = unpack(raw)
            return _strip_document_header(text), artifact

        async def _describe_images(path: str, image_paths: list[str]) -> list[str]:
            if image_tool is None or not image_paths:
                return []
            out: list[str] = []
            limit = int(getattr(settings, "document_image_max_items", 5))
            for index, image_path in enumerate(image_paths[:limit], 1):
                try:
                    raw = await asyncio.to_thread(
                        image_tool.invoke,
                        {
                            "path": image_path,
                            "reason": goal,
                            "question": f"提取第 {index} 张图片的内容",
                        },
                    )
                    text, _ = unpack(raw)
                    if text.strip():
                        out.append(f"[图片 {index}]\n{text}")
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"{path} 图片 {index} 理解失败："
                        f"{type(exc).__name__}: {exc}"
                    )
            return out

        async def _ocr_pages(path: str, pages: int) -> list[str]:
            if image_tool is None or page_tool is None:
                warnings.append(f"{path}: 扫描页缺少渲染/图像工具")
                return []
            out: list[str] = []
            limit = min(
                pages, int(getattr(settings, "document_ocr_max_pages", 5))
            )
            for page_no in range(1, limit + 1):
                try:
                    raw = await asyncio.to_thread(
                        page_tool.invoke,
                        {"path": path, "page": page_no, "reason": goal},
                    )
                    _, page_artifact = unpack(raw)
                    image_path = str(
                        (page_artifact or {}).get("image_path") or ""
                    )
                    if not image_path:
                        continue
                    raw = await asyncio.to_thread(
                        image_tool.invoke,
                        {
                            "path": image_path,
                            "reason": goal,
                            "question": f"提取第 {page_no} 页的文字与关键内容",
                        },
                    )
                    text, _ = unpack(raw)
                    if text.strip():
                        out.append(f"[PDF page={page_no} 视觉]\n{text}")
                except Exception as exc:  # noqa: BLE001 - 单页失败不拖垮
                    warnings.append(
                        f"{path} 第 {page_no} 页视觉理解失败："
                        f"{type(exc).__name__}: {exc}"
                    )
            return out

        for path in inputs:
            read_ranges: list[str] = []
            unread: list[str] = []
            doc_reason = ""
            doc_ok = False
            pages = 0
            try:
                collected: list[str] = []
                image_paths: list[str] = []
                ocr_needed = False
                if path.lower().endswith(".pdf"):
                    _, outline = await _read(
                        path, mode="outline", max_chars=4_000
                    )
                    pages = int((outline or {}).get("pages") or 0)
                    if outline and outline.get("ok") and pages > page_size:
                        if full_coverage:
                            page_cap = min(pages, page_cap_limit)
                        else:
                            page_cap = min(pages, page_size * max_calls)
                        calls = max(
                            1, (page_cap + page_size - 1) // page_size
                        )
                        for index in range(calls):
                            start = index * page_size + 1
                            end = min(page_cap, (index + 1) * page_size)
                            text, artifact = await _read(
                                path,
                                mode="auto",
                                page_range=f"{start}-{end}",
                                # 与单次读取上限一致：deep 模式下 5 页英文论文
                                # 常超过 12k，固定 12k 会把页范围整体标记为截断。
                                max_chars=non_pdf_max_chars,
                                include_images=True,
                                include_attachments=True,
                            )
                            if artifact:
                                ocr_needed = ocr_needed or bool(
                                    artifact.get("needs_ocr")
                                )
                                image_paths.extend(
                                    artifact.get("image_paths") or []
                                )
                            if artifact and artifact.get("ok") and text.strip():
                                collected.append(text)
                                read_ranges.append(f"{start}-{end}")
                                doc_ok = True
                                if artifact.get("truncated"):
                                    unread.append(
                                        f"{start}-{end}(截断)"
                                    )
                            else:
                                unread.append(f"{start}-{end}")
                                if not collected:
                                    warnings.append(f"{path}: {text[:300]}")
                        if page_cap < pages:
                            unread.append(f"{page_cap + 1}-{pages}")
                            doc_reason = (
                                f"未覆盖 {page_cap + 1}-{pages}"
                                f"（上限 {page_cap}/{pages} 页）"
                            )
                            warnings.append(f"{path}: {doc_reason}")
                else:
                    text, artifact = await _read(
                        path,
                        mode="auto",
                        max_chars=non_pdf_max_chars,
                        include_images=True,
                        include_attachments=True,
                    )
                    if artifact:
                        ocr_needed = bool(artifact.get("needs_ocr"))
                        image_paths.extend(artifact.get("image_paths") or [])
                    if artifact and artifact.get("ok") and text.strip():
                        collected.append(text)
                        read_ranges.append("all")
                        doc_ok = True
                        if artifact.get("truncated"):
                            unread.append("tail")
                            doc_reason = "文档被解析器截断"
                    elif not collected:
                        warnings.append(f"{path}: {text[:300]}")
                if not collected:
                    text, artifact = await _read(
                        path,
                        mode="auto",
                        max_chars=non_pdf_max_chars,
                        include_images=True,
                        include_attachments=True,
                    )
                    if artifact:
                        ocr_needed = bool(artifact.get("needs_ocr"))
                        image_paths.extend(artifact.get("image_paths") or [])
                    if artifact and artifact.get("ok") and text.strip():
                        collected.append(text)
                        read_ranges.append(
                            f"1-{pages}" if pages else "all"
                        )
                        doc_ok = True
                        if artifact.get("truncated"):
                            unread.append("tail")
                            doc_reason = "文档被解析器截断"
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{path}: {type(exc).__name__}: {exc}")
                doc_reason = f"{type(exc).__name__}: {exc}"
            if ocr_needed:
                if bool(getattr(settings, "document_ocr_enabled", False)):
                    collected.extend(await _ocr_pages(path, pages))
                else:
                    warnings.append(
                        f"{path}: 存在无文本层页面，默认仅处理文本型 PDF，"
                        "已跳过扫描页"
                    )
            if image_paths:
                collected.extend(await _describe_images(path, image_paths))
            if collected:
                body_text = "\n\n".join(collected)
                if not map_reduce:
                    body_text = body_text[:_MAX_SOURCE_CHARS]
                chunks.append(f"### 来源：{path}\n{body_text}")
                doc_units.append({"path": path, "text": body_text})
            else:
                warnings.append(f"{path}: 没有可用文本")
            coverage_docs.append(
                {
                    "path": path,
                    "pages": pages,
                    "read_ranges": read_ranges,
                    "unread": unread,
                    "status": (
                        "complete"
                        if doc_ok and not unread
                        else "partial"
                    ),
                    "reason": doc_reason,
                }
            )
            if unread:
                warnings.append(
                    f"{path}: 未覆盖页 " + "、".join(unread[:5])
                )
        if not chunks and dependency_context:
            context = _context_text(
                dependency_context,
                max_chars=int(
                    getattr(
                        settings,
                        "orchestrator_dependency_context_chars",
                        12_000,
                    )
                ),
            )
            chunks.append(f"### 上游任务汇总\n{context[:_MAX_SOURCE_CHARS]}")
        coverage_status = (
            "partial"
            if any(
                item.get("status") == "partial" for item in coverage_docs
            )
            else "complete"
        )
        coverage = {
            "status": coverage_status,
            "documents": coverage_docs,
        }
        coverage_findings = [
            f"覆盖：{item.get('path', '')} 已读 "
            f"{'、'.join(item.get('read_ranges') or []) or '无'}；未读 "
            f"{'、'.join(item.get('unread') or []) or '无'}"
            for item in coverage_docs
            if item.get("status") == "partial"
        ]

        if not chunks:
            return WorkerResult(
                task_id=task_id, role="document", worker_id=f"document:{task_id}",
                goal=goal, status="failed",
                summary="没有可分析的文档内容" + (
                    "；" + "；".join(warnings[:5]) if warnings else ""
                ),
                files=inputs,
                coverage=coverage,
            )

        def _body(text: str) -> str:
            return (
                f"任务：{goal}\n用户原始请求：{parent_request}\n"
                + (f"会话约束：{session_brief}\n" if session_brief else "")
                + "\n文档内容：\n"
                + text
            )

        async def _analyze(
            text: str,
        ) -> tuple[DocumentAnalysis, int, int]:
            payload = await structured.ainvoke(
                [
                    SystemMessage(content=DOCUMENT_PROMPT),
                    HumanMessage(content=_body(text)),
                ],
                config,
            )
            used_in = used_out = 0
            if isinstance(payload, DocumentAnalysis):
                return payload, 0, 0
            if isinstance(payload, dict):
                parsed = payload.get("parsed")
                raw = payload.get("raw")
                if raw is not None:
                    used_in, used_out = _usage(raw)
                if parsed is None:
                    raw_text = str(
                        getattr(raw, "content", "") or ""
                    ).strip()
                    raise ValueError(
                        "结构化输出为空"
                        + (f"：{raw_text[:300]}" if raw_text else "")
                    )
                analysis = (
                    parsed
                    if isinstance(parsed, DocumentAnalysis)
                    else DocumentAnalysis.model_validate(parsed)
                )
                return analysis, used_in, used_out
            return DocumentAnalysis.model_validate(payload), 0, 0

        async def _consolidate(
            text: str, prompt: str = CONSOLIDATE_PROMPT
        ) -> tuple[str, int, int]:
            """合并摘要/矩阵节点；解析失败时尽量从截断 JSON 里捞出 summary。"""
            payload = await consolidator.ainvoke(
                [
                    SystemMessage(content=prompt),
                    HumanMessage(content=_body(text)),
                ],
                config,
            )
            used_in = used_out = 0
            if isinstance(payload, SummaryOnly):
                return payload.summary.strip(), 0, 0
            if not isinstance(payload, dict):
                return "", 0, 0
            raw = payload.get("raw")
            if raw is not None:
                used_in, used_out = _usage(raw)
            parsed = payload.get("parsed")
            if isinstance(parsed, SummaryOnly):
                return parsed.summary.strip(), used_in, used_out
            if isinstance(parsed, dict):
                return (
                    SummaryOnly.model_validate(parsed).summary.strip(),
                    used_in,
                    used_out,
                )
            raw_text = str(getattr(raw, "content", "") or "")
            match = _SUMMARY_FIELD_RE.search(raw_text)
            if match:
                try:
                    return (
                        json.loads(f'"{match.group(1)}"').strip(),
                        used_in,
                        used_out,
                    )
                except json.JSONDecodeError:
                    pass
            return "", used_in, used_out

        async def _build_matrix(
            path: str,
            doc_analyses: list[DocumentAnalysis],
            doc_sources: list[dict[str, Any]],
        ) -> tuple[DocumentMatrix, int, int]:
            """把单篇的分片摘要压成固定槽位；失败时退化为证据要点。"""
            stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            summaries = "\n".join(
                item.summary.strip()
                for item in doc_analyses
                if item.summary.strip()
            )
            evidence_lines = "\n".join(
                f"- {source.get('claim', '')} "
                f"[{source.get('path', '')}#{source.get('anchor', '')}] "
                f"{str(source.get('quote', ''))[:200]}"
                for source in doc_sources[:12]
            )
            text = (
                f"路径：{path}\n\n摘要：\n{summaries[:20_000]}\n\n"
                f"证据：\n{evidence_lines}"
            )
            limit = max(
                2_000,
                int(
                    getattr(
                        settings, "document_tree_input_chars", 16_000
                    )
                    or 16_000
                ),
            )
            used_in = used_out = 0
            try:
                payload = await matrix_structured.ainvoke(
                    [
                        SystemMessage(content=MATRIX_PROMPT),
                        HumanMessage(content=_body(text[:limit])),
                    ],
                    config,
                )
                if isinstance(payload, dict):
                    raw = payload.get("raw")
                    parsed = payload.get("parsed")
                    if raw is not None:
                        used_in, used_out = _usage(raw)
                    if isinstance(parsed, DocumentMatrix):
                        entry = parsed
                    elif isinstance(parsed, dict):
                        entry = DocumentMatrix.model_validate(parsed)
                    else:
                        entry = None
                elif isinstance(payload, DocumentMatrix):
                    entry = payload
                else:
                    entry = None
            except Exception as exc:  # noqa: BLE001 - 矩阵是增强，不阻断
                warnings.append(
                    f"{path} 矩阵提炼失败：{type(exc).__name__}: {exc}"
                )
                entry = None
            if entry is None:
                entry = DocumentMatrix(
                    path=path,
                    title=stem,
                    method="；".join(
                        str(source.get("claim", ""))
                        for source in doc_sources[:5]
                    ),
                    evidence=[
                        DocumentPoint(
                            claim=str(source.get("claim", "")),
                            evidence_path=str(source.get("path", "")),
                            anchor=str(source.get("anchor", "")),
                            quote=str(source.get("quote", "")),
                            confidence=str(
                                source.get("confidence", "medium")
                            ),
                        )
                        for source in doc_sources[:8]
                    ],
                )
            updates: dict[str, Any] = {
                "path": path,
                "title": entry.title.strip() or stem,
            }
            for key, _label in MATRIX_SLOTS:
                updates[key] = _sanitize_citations(
                    getattr(entry, key, ""), doc_sources
                )
            for key, hints in _MATRIX_SLOT_HINTS.items():
                if str(updates.get(key, "")).strip():
                    continue
                matched = [
                    str(source.get("claim", "")).strip()
                    for source in doc_sources
                    if str(source.get("claim", "")).strip()
                    and any(
                        hint.lower()
                        in str(source.get("claim", "")).lower()
                        for hint in hints
                    )
                ]
                if matched:
                    updates[key] = "；".join(
                        dict.fromkeys(matched[:3])
                    )
            if not entry.evidence:
                updates["evidence"] = [
                    DocumentPoint(
                        claim=str(source.get("claim", "")),
                        evidence_path=str(source.get("path", "")),
                        anchor=str(source.get("anchor", "")),
                        quote=str(source.get("quote", "")),
                        confidence=str(source.get("confidence", "medium")),
                    )
                    for source in doc_sources[:8]
                ]
            return entry.model_copy(update=updates), used_in, used_out

        async def _tree_reduce(
            nodes: list[str],
        ) -> tuple[str, int, int]:
            """分层归并：每层按 fanout 分组的节点数下降，直到一个根节点。"""
            fanout = max(
                2,
                int(getattr(settings, "document_tree_fanout", 5) or 5),
            )
            limit = max(
                2_000,
                int(
                    getattr(
                        settings, "document_tree_input_chars", 16_000
                    )
                    or 16_000
                ),
            )
            used_in = used_out = 0
            current = [node for node in nodes if node.strip()]
            while len(current) > 1:
                merged: list[str] = []
                for start in range(0, len(current), fanout):
                    group = current[start : start + fanout]
                    if len(group) == 1:
                        merged.append(group[0])
                        continue
                    text = "\n\n".join(group)[:limit]
                    summary, node_in, node_out = await _consolidate(
                        text, TREE_MERGE_PROMPT
                    )
                    used_in += node_in
                    used_out += node_out
                    merged.append(
                        summary or "\n\n".join(group)[:4_000]
                    )
                if len(merged) == len(current):
                    # 防御：节点数不再下降时停止，避免死循环。
                    break
                current = merged
            return (
                current[0] if current else "",
                used_in,
                used_out,
            )

        input_tokens = 0
        output_tokens = 0
        map_partial = False
        points: list[DocumentPoint] = []
        # 多文档联合分析时，证据总量随文档数放大；固定 200 会截掉后页引用。
        point_limit = min(20_000, max(200, 800 * max(1, len(inputs))))
        # 最终证据列表按文档轮转取样，避免一份长文档占满上限。
        source_limit = min(600, max(200, 120 * max(1, len(inputs))))
        analyses: list[DocumentAnalysis] = []
        analysis_paths: list[str] = []
        summary_nodes: list[str] = []
        matrix_entries: list[dict[str, Any]] = []
        attribution_issues: list[dict[str, Any]] = []
        try:
            if map_reduce:
                units: list[str] = []
                unit_paths: list[str] = []
                for unit in doc_units:
                    path = str(unit.get("path", ""))
                    parts = _split_sections(
                        str(unit.get("text", "")), map_chunk_chars
                    )
                    units.extend(parts)
                    unit_paths.extend([path] * len(parts))
                if not units:
                    for chunk in chunks:
                        parts = _split_sections(chunk, map_chunk_chars)
                        units.extend(parts)
                        unit_paths.extend([""] * len(parts))
                budget = max(
                    4_000,
                    int(
                        getattr(
                            settings, "document_max_chars", 2_000_000
                        )
                        or 2_000_000
                    ),
                )
                kept: list[str] = []
                kept_paths: list[str] = []
                used = 0
                for unit, path in zip(units, unit_paths, strict=True):
                    if used + len(unit) > budget:
                        map_partial = True
                        warnings.append(
                            f"map 文本超过 {budget} 字符，后续片段未处理"
                        )
                        break
                    kept.append(unit)
                    kept_paths.append(path)
                    used += len(unit)
                units = kept
                unit_paths = kept_paths
                semaphore = asyncio.Semaphore(map_concurrency)

                async def _one(
                    text: str,
                ) -> tuple[DocumentAnalysis, int, int]:
                    async with semaphore:
                        return await _analyze(text)

                outcomes = await asyncio.gather(
                    *(_one(unit) for unit in units), return_exceptions=True
                )
                for path, item in zip(unit_paths, outcomes, strict=True):
                    if isinstance(item, tuple):
                        analyses.append(item[0])
                        analysis_paths.append(path)
                        input_tokens += item[1]
                        output_tokens += item[2]
                    elif isinstance(item, Exception):
                        map_partial = True
                        warnings.append(
                            "map 片段失败："
                            f"{type(item).__name__}: {item}"
                        )
                if analyses:
                    analysis = _merge_analyses(
                        analyses, point_limit=point_limit
                    )
                    summaries_by_path: dict[str, list[str]] = {}
                    for path, item in zip(analysis_paths, analyses, strict=True):
                        summary = item.summary.strip()
                        if summary:
                            summaries_by_path.setdefault(path, []).append(
                                summary
                            )
                    summary_nodes: list[str] = []
                    position = 0
                    while True:
                        added = False
                        for path, items in summaries_by_path.items():
                            if position < len(items):
                                label = path or "未知来源"
                                summary_nodes.append(
                                    f"## {label}\n{items[position]}"
                                )
                                added = True
                        if not added:
                            break
                        position += 1
                    if summary_nodes:
                        analysis = analysis.model_copy(
                            update={
                                "summary": "\n\n".join(
                                    summary_nodes
                                )[:6_000]
                            }
                        )
                else:
                    # 所有分批都失败时退回整篇一次调用（仍有上限）。
                    analysis, used_in, used_out = await _analyze(
                        "\n\n".join(units)[:_MAX_SOURCE_CHARS]
                    )
                    input_tokens += used_in
                    output_tokens += used_out
                    map_partial = True
            else:
                analysis, used_in, used_out = await _analyze(
                    "\n\n".join(chunks)
                )
                input_tokens += used_in
                output_tokens += used_out
            points = list(analysis.points)
            summary = analysis.summary.strip() or "（无摘要）"
            candidate_sources = _verify_source_quotes(
                [
                    {
                        "path": _resolve_evidence_path(
                            point.evidence_path,
                            point.quote,
                            doc_units,
                            inputs,
                        ),
                        "anchor": point.anchor,
                        "claim": point.claim,
                        "quote": point.quote[:300],
                        "confidence": point.confidence,
                    }
                    for point in analysis.points[:point_limit]
                    if point.claim
                ],
                doc_units,
            )
            # 只把“原文能定位”的证据交给下游；完全对不上的点不进入引用集，
            # 避免 author 把章节号/作者名当成锚点照抄。
            verified_sources = [
                source
                for source in candidate_sources
                if source.get("quote_verified") is True
                and source.get("anchor_valid") is not False
                and _is_plausible_anchor(str(source.get("anchor", "")))
            ]
            usable = verified_sources or [
                source
                for source in candidate_sources
                if source.get("path")
                and source.get("anchor_valid") is not False
                and _is_plausible_anchor(str(source.get("anchor", "")))
            ]
            sources = _balanced_sources(usable, source_limit)
            findings = [
                str(source.get("claim", ""))
                for source in sources[:20]
                if source.get("claim")
            ] + coverage_findings
            matrix_enabled = bool(
                getattr(settings, "document_matrix_enabled", False)
            )
            if matrix_enabled and inputs:
                matrix_sem = asyncio.Semaphore(map_concurrency)

                async def _matrix_one(
                    path: str,
                ) -> tuple[DocumentMatrix, int, int]:
                    async with matrix_sem:
                        doc_analyses = [
                            item
                            for item, item_path in zip(
                                analyses, analysis_paths, strict=True
                            )
                            if item_path == path
                        ]
                        doc_sources = [
                            source
                            for source in sources
                            if str(source.get("path", "")) == path
                        ][:12]
                        return await _build_matrix(
                            path, doc_analyses, doc_sources
                        )

                matrix_outcomes = await asyncio.gather(
                    *(_matrix_one(path) for path in inputs),
                    return_exceptions=True,
                )
                for path, item in zip(
                    inputs, matrix_outcomes, strict=True
                ):
                    if isinstance(item, tuple):
                        entry, used_in, used_out = item
                        matrix_entries.append(entry.model_dump())
                        input_tokens += used_in
                        output_tokens += used_out
                    else:
                        warnings.append(
                            f"{path} 矩阵提炼失败："
                            f"{type(item).__name__}: {item}"
                        )
            matrix_nodes = [
                _render_matrix(entry) for entry in matrix_entries
            ]
            tree_nodes = (
                matrix_nodes if len(inputs) > 1 else summary_nodes
            ) or summary_nodes
            if tree_nodes:
                merged, used_in, used_out = await _tree_reduce(tree_nodes)
                input_tokens += used_in
                output_tokens += used_out
                if merged:
                    summary = merged
            if len(inputs) > 1 and matrix_entries:
                compact = "\n".join(
                    line
                    for line in (
                        _render_matrix_compact(entry)
                        for entry in matrix_entries
                    )
                    if line
                )
                if compact and "## 证据矩阵" not in summary:
                    summary = (
                        summary.rstrip()
                        + "\n\n## 证据矩阵\n"
                        + compact
                    )
            if len(inputs) > 1 and matrix_entries:
                parsed = "、".join(
                    str(entry.get("path", ""))
                    + (
                        f"（{entry.get('title')}）"
                        if entry.get("title")
                        else ""
                    )
                    for entry in matrix_entries
                )
                if parsed and not summary.startswith("已解析文档"):
                    summary = (
                        f"已解析文档（{len(matrix_entries)} 篇）："
                        f"{parsed}\n\n{summary}"
                    )
            attribution_issues = find_attribution_issues(
                summary,
                [
                    {
                        "path": str(entry.get("path", "")),
                        "title": str(entry.get("title", "")),
                    }
                    for entry in matrix_entries
                ]
                or [{"path": path} for path in inputs],
            )
            if attribution_issues:
                findings.append(
                    f"归属校验：{len(attribution_issues)} 处跨文档引用不一致"
                )
            summary = _sanitize_citations(
                _append_evidence_refs(
                    _normalize_citation_paths(
                        summary, doc_units, inputs, sources
                    ),
                    sources,
                ),
                sources,
            )
            status = (
                "partial"
                if coverage_status == "partial" or map_partial
                else "success"
            )
        except Exception as exc:  # noqa: BLE001 - LLM 失败时保留原始文本
            summary = (
                "结构化抽取失败，保留文档摘要："
                f"{type(exc).__name__}: {exc}\n"
                + "\n".join(chunks)[:2_000]
            )
            findings = coverage_findings
            sources = []
            status = "partial"

        run_id = str(task.get("knowledge_run_id") or "")
        if knowledge_store is not None and run_id and doc_units:
            try:
                coverage_by_path = {
                    str(item.get("path", "")): item
                    for item in coverage_docs
                }
                chunk_payload: list[dict[str, Any]] = []
                for unit in doc_units:
                    path = str(unit.get("path", ""))
                    for index, part in enumerate(
                        _split_sections(
                            str(unit.get("text", "")),
                            min(map_chunk_chars, 4_000),
                        ),
                        1,
                    ):
                        chunk_payload.append(
                            {
                                "chunk_id": f"{task_id}-{index}",
                                "path": path,
                                "anchor": f"part-{index}",
                                "text": part,
                            }
                        )
                claims = [
                    {
                        "claim": point.claim,
                        "evidence_path": _resolve_evidence_path(
                            point.evidence_path,
                            point.quote,
                            doc_units,
                            inputs,
                        ),
                        "anchor": point.anchor,
                        "quote": point.quote,
                        "confidence": point.confidence,
                    }
                    for point in points
                    if point.claim
                ]
                saved = knowledge_store.save_doc_index(
                    run_id,
                    task_id,
                    documents=[
                        coverage_by_path.get(str(unit["path"]))
                        or {"path": unit["path"]}
                        for unit in doc_units
                    ],
                    chunks=chunk_payload,
                    summary=summary,
                )
                if not saved:
                    warnings.append("知识产物落盘失败（doc）")
                knowledge_store.save_claims(run_id, task_id, claims)
            except Exception as exc:  # noqa: BLE001 - 产物是增强，不阻断结论
                warnings.append(
                    f"知识产物落盘失败：{type(exc).__name__}: {exc}"
                )
        return WorkerResult(
            task_id=task_id,
            role="document",
            worker_id=f"document:{task_id}",
            goal=goal,
            status=status,
            summary=summary,
            files=inputs,
            findings=findings,
            sources=sources,
            coverage=coverage,
            matrix=matrix_entries,
            attribution_issues=attribution_issues,
            verification={
                "matrix_entries": len(matrix_entries),
                "attribution_issues": len(attribution_issues),
            },
            duration_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error="；".join(warnings[:5]),
        )

    return run


def make_vision_runner(*, tools: list[Any]) -> Any:
    async def run(
        task: dict[str, Any],
        *,
        parent_request: str,
        dependency_context: dict[str, Any] | None = None,
        cancel: Any = None,
        **_: Any,
    ) -> WorkerResult:
        started = time.perf_counter()
        task_id = str(task.get("id", ""))
        goal = str(task.get("goal", ""))
        inputs = [str(item) for item in (task.get("inputs") or [])][:_MAX_FILES]
        tool = _find_tool(tools, IMAGE_INSPECT)
        if tool is None:
            return WorkerResult(
                task_id=task_id, role="vision", worker_id=f"vision:{task_id}",
                goal=goal, status="failed", summary="image_inspect 工具未配置",
            )
        summaries: list[str] = []
        findings: list[str] = []
        warnings: list[str] = []
        for path in inputs:
            if cancel is not None and cancel():
                return WorkerResult(
                    task_id=task_id, role="vision", worker_id=f"vision:{task_id}",
                    goal=goal, status="cancelled", summary="已取消",
                )
            try:
                raw = await asyncio.to_thread(
                    tool.invoke,
                    {"path": path, "reason": goal, "question": goal},
                )
                text, artifact = unpack(raw)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if artifact and not artifact.get("ok"):
                warnings.append(f"{path}: {text[:300]}")
                continue
            summaries.append(text)
            findings.extend(
                str(item) for item in (artifact or {}).get("findings") or []
            )
        if not summaries:
            return WorkerResult(
                task_id=task_id, role="vision", worker_id=f"vision:{task_id}",
                goal=goal, status="failed",
                summary="没有成功理解的图片" + (
                    "；" + "；".join(warnings[:5]) if warnings else ""
                ),
                files=inputs,
            )
        return WorkerResult(
            task_id=task_id,
            role="vision",
            worker_id=f"vision:{task_id}",
            goal=goal,
            status="success" if not warnings else "partial",
            summary="\n\n".join(summaries)[:4_000],
            files=inputs,
            findings=findings[:20],
            sources=[
                {"path": path, "anchor": "image", "quote": ""}
                for path in inputs
            ],
            duration_ms=int((time.perf_counter() - started) * 1000),
            error="；".join(warnings[:5]),
        )

    return run


def make_author_runner(
    *,
    llm: BaseChatModel,
    tools: list[Any],
    settings: Any,
    knowledge_store: Any | None = None,
) -> Any:
    async def run(
        task: dict[str, Any],
        *,
        parent_request: str,
        dependency_context: dict[str, Any] | None = None,
        config: RunnableConfig | None = None,
        cancel: Any = None,
        **_: Any,
    ) -> WorkerResult:
        started = time.perf_counter()
        task_id = str(task.get("id", ""))
        goal = str(task.get("goal", ""))
        if cancel is not None and cancel():
            return WorkerResult(
                task_id=task_id, role="author", worker_id=f"author:{task_id}",
                goal=goal, status="cancelled", summary="已取消",
            )
        fmt = str(task.get("output_format") or "").strip().lower()
        if fmt not in {"md", "docx", "pdf"}:
            fmt = resolve_output_format(goal, parent_request)
        report_dir = str(
            getattr(settings, "report_output_dir", ".agent/artifacts/reports")
        ).strip("/")
        out_path = str(task.get("output_path") or "").strip()
        if not out_path:
            out_path = f"{report_dir}/report.{fmt}"
        # 路径归一化后再校验：报告工具本身也会拒绝越界，但 worker 层
        # 先拦一次能给出更明确的失败原因，且不依赖工具的实现细节。
        out_path = out_path.replace("\\", "/")
        while out_path.startswith("./"):
            out_path = out_path[2:]
        scopes = [
            str(scope).strip().replace("\\", "/").rstrip("/")
            for scope in (task.get("write_scope") or [])
            if str(scope).strip()
        ]
        if (
            out_path.startswith("/")
            or ".." in out_path.split("/")
            or (scopes and not _within_scope(out_path, scopes))
        ):
            return WorkerResult(
                task_id=task_id,
                role="author",
                worker_id=f"author:{task_id}",
                goal=goal,
                status="failed",
                summary=f"报告输出不在 write_scope 内：{out_path}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        tool = _find_tool(tools, REPORT_EXPORT)
        if tool is None:
            return WorkerResult(
                task_id=task_id, role="author", worker_id=f"author:{task_id}",
                goal=goal, status="failed", summary="report_export 工具未配置",
            )

        dependency_paths = {
            str(source.get("path", "")).strip()
            for entry in (dependency_context or {}).get("tasks") or []
            for source in entry.get("sources") or []
            if isinstance(source, dict) and str(source.get("path", "")).strip()
        }
        # 多文档时至少给每篇留 6 条证据，避免只看到第一篇。
        source_limit = max(
            int(getattr(settings, "author_evidence_top_k", 12) or 12),
            6 * max(1, len(dependency_paths)),
        )
        if bool(getattr(settings, "deep_workflows_enabled", False)):
            context = _context_text(
                dependency_context or {},
                max_chars=int(
                    getattr(
                        settings, "author_evidence_max_chars", 24_000
                    )
                    or 24_000
                ),
                source_limit=source_limit,
                quote_chars=500,
            )
        else:
            context = _context_text(
                dependency_context or {},
                max_chars=int(
                    getattr(
                        settings,
                        "orchestrator_dependency_context_chars",
                        12_000,
                    )
                ),
                source_limit=source_limit,
            )
        run_id = str(task.get("knowledge_run_id") or "")
        if knowledge_store is not None and run_id:
            try:
                from ubismart.knowledge.embedding import (
                    make_knowledge_embedder,
                )

                embedder, _ = make_knowledge_embedder(settings)
                results = knowledge_store.search(
                    run_id,
                    f"{goal} {parent_request}",
                    top_k=int(
                        getattr(settings, "author_evidence_top_k", 12) or 12
                    ),
                    embedder=embedder,
                    mode=str(
                        getattr(
                            settings, "knowledge_search_mode", "lexical"
                        )
                        or "lexical"
                    ),
                )
                if results:
                    context = (
                        "宿主自动检索到的证据（优先使用）：\n"
                        + knowledge_store.render_search(run_id, results)
                        + "\n\n"
                        + context
                    )
            except Exception:  # noqa: BLE001 - 检索是增强，不阻断写作
                pass
        required_sections = [
            str(item).strip()
            for item in (task.get("required_sections") or [])
            if str(item).strip()
        ]
        body = (
            f"用户原始请求：{parent_request}\n任务：{goal}\n"
            f"输出格式：{fmt}\n\n可用的上游证据：\n{context}"
        )
        if required_sections:
            body += (
                "\n\n必须包含以下 Markdown 二级标题（一字不差）："
                + "、".join(required_sections)
            )
        response = None
        try:
            response = await llm.ainvoke(
                [SystemMessage(content=AUTHOR_PROMPT), HumanMessage(content=body)],
                config,
            )
            content = getattr(response, "content", "")
            if not isinstance(content, str):
                content = str(content)
            content = content.strip()
        except Exception:  # noqa: BLE001 - 回退到确定性拼装
            content = ""
        if not content:
            content = (
                f"# {goal}\n\n"
                "## 关键信息\n\n"
                + "\n".join(
                    f"- {line}" for line in context.splitlines() if line.strip()
                )
                + "\n\n## 局限性\n\n本报告由上游摘要自动拼装，未经过模型润色。\n"
            )
        sources = [
            dict(source)
            for entry in (dependency_context or {}).get("tasks") or []
            for source in (entry.get("sources") or [])
            if isinstance(source, dict)
        ]
        content = _sanitize_citations(content, sources)
        if sources:
            seen: set[str] = set()
            lines: list[str] = []
            for source in sources:
                ref = f"{source.get('path', '')}#{source.get('anchor', '')}"
                if not ref.strip("#") or ref in seen:
                    continue
                seen.add(ref)
                lines.append(f"- {ref}")
            if lines:
                content = (
                    content.rstrip()
                    + "\n\n## 参考来源\n\n"
                    + "\n".join(lines)
                    + "\n"
                )
        try:
            raw = await asyncio.to_thread(
                tool.invoke,
                {
                    "content": content,
                    "out_path": out_path,
                    "reason": goal,
                    "format": fmt,
                    "title": goal[:120],
                    "template": str(task.get("template") or ""),
                    "required_sections": [
                        str(item)
                        for item in (task.get("required_sections") or [])
                        if str(item).strip()
                    ],
                },
            )
            text, artifact = unpack(raw)
        except Exception as exc:  # noqa: BLE001
            return WorkerResult(
                task_id=task_id, role="author", worker_id=f"author:{task_id}",
                goal=goal, status="failed",
                summary=f"报告导出失败：{type(exc).__name__}: {exc}",
                error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        if not artifact or not artifact.get("ok"):
            return WorkerResult(
                task_id=task_id, role="author", worker_id=f"author:{task_id}",
                goal=goal, status="failed", summary=text[:1_500],
                error=text[:1_500],
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        findings = [f"输出格式：{fmt}"]
        check_error = ""
        checker = _find_tool(tools, REPORT_CHECK)
        if checker is not None:
            try:
                check_text, check = unpack(
                    await asyncio.to_thread(
                        checker.invoke, {"path": out_path, "reason": goal}
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 自检失败不丢报告
                check_text, check = "", {"ok": False, "warnings": [str(exc)]}
            if not check or not check.get("ok"):
                warnings = [
                    str(item) for item in (check or {}).get("warnings") or []
                ]
                check_error = "；".join(warnings)[:500] or check_text[:500]
                findings.append("报告自检未通过：" + check_error)
            else:
                findings.append("报告自检通过")
        input_tokens, output_tokens = _usage(response) if response is not None else (0, 0)
        return WorkerResult(
            task_id=task_id,
            role="author",
            worker_id=f"author:{task_id}",
            goal=goal,
            status="partial" if check_error else "success",
            summary=f"已生成 {fmt.upper()} 报告：{out_path}"
            + (f"（自检：{check_error}）" if check_error else ""),
            files=[out_path],
            findings=findings,
            output_format=fmt,
            output_path=out_path,
            error=check_error,
            sources=[
                dict(source)
                for entry in (dependency_context or {}).get("tasks") or []
                for source in (entry.get("sources") or [])
                if isinstance(source, dict)
            ][:50],
            duration_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    return run


def make_report_review_runner(
    *,
    tools: list[Any],
    llm: Any | None = None,
    settings: Any | None = None,
) -> Any:
    """文档管线的 review：对报告执行确定性 report_check。"""

    async def run(
        task: dict[str, Any],
        *,
        parent_request: str,
        dependency_context: dict[str, Any] | None = None,
        cancel: Any = None,
        **_: Any,
    ) -> WorkerResult:
        started = time.perf_counter()
        task_id = str(task.get("id", ""))
        goal = str(task.get("goal", ""))
        if cancel is not None and cancel():
            return WorkerResult(
                task_id=task_id, role="review", worker_id=f"review:{task_id}",
                goal=goal, status="cancelled", summary="已取消",
            )
        paths = [
            str(item)
            for item in (task.get("inputs") or [])
            if str(item).lower().endswith((".md", ".docx", ".pdf"))
        ]
        if not paths:
            # 没有显式输入时才从依赖结果里找；不要把上游 PDF 当成报告复核。
            for entry in (dependency_context or {}).get("tasks") or []:
                for path in entry.get("files") or []:
                    text = str(path)
                    if text.lower().endswith((".md", ".docx", ".pdf")):
                        paths.append(text)
        paths = list(dict.fromkeys(paths))
        tool = _find_tool(tools, REPORT_CHECK)
        if tool is None:
            return WorkerResult(
                task_id=task_id, role="review", worker_id=f"review:{task_id}",
                goal=goal, status="failed", summary="report_check 工具未配置",
            )
        if not paths:
            return WorkerResult(
                task_id=task_id, role="review", worker_id=f"review:{task_id}",
                goal=goal, status="failed",
                summary="没有可校验的报告路径",
            )
        findings: list[str] = []
        failed: list[str] = []
        for path in paths:
            try:
                text, artifact = unpack(
                    await asyncio.to_thread(
                        tool.invoke,
                        {
                            "path": path,
                            "reason": goal,
                            "required_sections": [
                                str(item)
                                for item in (
                                    task.get("required_sections") or []
                                )
                                if str(item).strip()
                            ],
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if artifact and artifact.get("ok"):
                findings.append(f"{path} 校验通过")
            else:
                warnings = [
                    str(item)
                    for item in (artifact or {}).get("warnings") or []
                ]
                failed.append(f"{path}: " + ("；".join(warnings) or text[:200]))
        status = "success" if not failed else ("partial" if not findings else "failed")
        # 全部失败才算 failed；部分通过按 partial 处理。
        if findings and not failed:
            status = "success"
        elif findings:
            status = "partial"
        else:
            status = "failed"

        # 可选的 LLM 证据审查：只判断报告结论是否能被引用/上游证据支持。
        if (
            status != "failed"
            and llm is not None
            and settings is not None
            and bool(getattr(settings, "report_llm_review", False))
        ):
            read_tool = _find_tool(tools, REPORT_READ)
            if read_tool is not None:
                report_texts: list[str] = []
                for path in paths:
                    try:
                        text, artifact = unpack(
                            await asyncio.to_thread(
                                read_tool.invoke,
                                {"path": path, "reason": goal},
                            )
                        )
                        if artifact and artifact.get("ok"):
                            report_texts.append(text)
                    except Exception:  # noqa: BLE001
                        continue
                if report_texts:
                    evidence = _context_text(
                        dependency_context or {},
                        max_chars=int(
                            getattr(
                                settings,
                                "orchestrator_dependency_context_chars",
                                12_000,
                            )
                        ),
                    )
                    report_body = "\n\n".join(report_texts)[:24_000]
                    prompt = (
                        "判断报告中的关键结论是否能被引用锚点或上游证据支持。"
                        "只输出 JSON：{\"supported\": true/false, "
                        "\"issues\": [...], \"summary\": \"...\"}\n\n"
                        f"报告：\n{report_body}\n\n"
                        f"上游证据：\n{evidence[:8_000]}"
                    )
                    try:
                        structured = with_json_output(llm, EvidenceReview)
                        result = await structured.ainvoke(
                            [
                                SystemMessage(
                                    content="你是报告证据审查器，只依据给定材料判断。"
                                ),
                                HumanMessage(content=prompt),
                            ]
                        )
                        review = (
                            result
                            if isinstance(result, EvidenceReview)
                            else EvidenceReview.model_validate(result)
                        )
                        if not review.supported or review.issues:
                            status = "partial" if status == "success" else status
                            detail = "；".join(review.issues[:5]) or review.summary
                            findings.append(f"LLM 证据审查未通过：{detail}"[:1_500])
                        else:
                            findings.append("LLM 证据审查通过")
                    except Exception as exc:  # noqa: BLE001 - 审查失败不否定确定性结果
                        findings.append(
                            "LLM 证据审查失败（已忽略）："
                            f"{type(exc).__name__}: {exc}"[:1_000]
                        )
        return WorkerResult(
            task_id=task_id,
            role="review",
            worker_id=f"review:{task_id}",
            goal=goal,
            status=status,
            summary=(
                "报告校验通过"
                if status == "success"
                else "报告校验问题：" + "；".join(failed)[:1_500]
            ),
            files=paths,
            findings=findings + failed,
            error="；".join(failed)[:1_500],
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    return run
