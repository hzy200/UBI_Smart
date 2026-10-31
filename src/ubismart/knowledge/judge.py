"""文档答案的自动 judge（RFC-0009 / ADR-0034）。

两层：
- 确定性：引用是否有 path/anchor/quote、答案里引用的锚点是否都能在证据里找到；
- LLM（可选）：判断每条结论是否被证据支持，输出阻塞项与分数。

LLM 失败回退确定性结果，不把“judge 挂了”误判成“答案很差”。
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from ubismart.llm.deepseek import with_json_output

# 显式引用：锚点允许空格（如 "page=8 Table 2"）。
_BRACKET_REF_RE = re.compile(
    r"\[来源\s*[:：]\s*([^\]#]+?)#([^\]]+?)\s*\]"
)
# 裸引用：路径必须带扩展名，避免把 Markdown 标题（##）误当成引用。
_BARE_REF_RE = re.compile(
    r"([^\s\[\]#]+?\.[A-Za-z0-9]{1,8})#([^\s\]，。;]+)"
)


class DocumentJudgement(BaseModel):
    score: int = Field(default=0, ge=0, le=100)
    supported: bool = False
    unsupported_claims: list[str] = Field(default_factory=list)
    citation_issues: list[str] = Field(default_factory=list)
    summary: str = ""


JUDGE_PROMPT = """\
你是文档问答的严格评审。只依据给出的证据判断答案，不要使用外部知识。

要求：
- 逐条检查答案中的关键结论是否有证据（path#anchor + 原文）支撑；
- 没有证据、或证据与结论不一致的，写入 unsupported_claims；
- 引用路径/锚点无法对应到证据的，写入 citation_issues；
- score 0-100：100 = 所有关键结论都有可核验证据；60 以下视为不可信；
- 只输出 JSON：{"score": 0, "supported": false,
  "unsupported_claims": [], "citation_issues": [], "summary": ""}
"""


def _source_metrics(sources: list[dict[str, Any]]) -> dict[str, Any]:
    items = [item for item in (sources or []) if isinstance(item, dict)]
    total = len(items)
    if not total:
        return {
            "citations": 0,
            "anchor_ratio": 0.0,
            "quote_ratio": 0.0,
            "quote_verified_ratio": 0.0,
            "unverified_quotes": [],
            "known_refs": set(),
        }
    anchors = {
        f"{str(item.get('path', '')).strip()}#{str(item.get('anchor', '')).strip()}"
        for item in items
        if str(item.get("path", "")).strip()
        and str(item.get("anchor", "")).strip()
    }
    with_anchor = sum(
        1
        for item in items
        if str(item.get("path", "")).strip()
        and str(item.get("anchor", "")).strip()
    )
    verified_quotes = sum(
        1
        for item in items
        if str(item.get("quote", "")).strip()
        and item.get("quote_verified") is not False
    )
    unverified_quotes = sorted(
        f"{str(item.get('path', '')).strip()}"
        f"#{str(item.get('anchor', '')).strip()}"
        for item in items
        if item.get("quote_verified") is False
    )
    return {
        "citations": total,
        # 按“条目是否带锚点”计分；同一页被多条结论引用不应重复扣分。
        "anchor_ratio": with_anchor / total,
        "quote_ratio": verified_quotes / total,
        "quote_verified_ratio": verified_quotes / total,
        "unverified_quotes": unverified_quotes,
        "known_refs": anchors,
    }


def _extract_refs(text: str) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for path, anchor in _BRACKET_REF_RE.findall(str(text or "")):
        path, anchor = path.strip(), anchor.strip()
        if path and anchor:
            refs.add((path, anchor))
    for path, anchor in _BARE_REF_RE.findall(str(text or "")):
        path, anchor = path.strip(), anchor.strip()
        if path and anchor:
            refs.add((path, anchor))
    return refs


def _ref_is_known(
    path: str, anchor: str, known: set[str]
) -> bool:
    """证据锚点允许粗细粒度差异。

    例如证据抽到 `page=8 Table 2`，答案写 `page=8` 或 `Table 2` 都指向
    同一段原文，不应被判成“引用不存在”；反之，编造的新页码仍会失败。
    """
    if f"{path}#{anchor}" in known:
        return True
    for ref in known:
        known_path, _, known_anchor = ref.partition("#")
        if known_path != path:
            continue
        if anchor in known_anchor or known_anchor in anchor:
            return True
    return False


def deterministic_judgement(
    answer: str,
    sources: list[dict[str, Any]],
    *,
    pass_score: int = 60,
) -> dict[str, Any]:
    metrics = _source_metrics(sources)
    ref_items = _extract_refs(str(answer or ""))
    refs = {f"{path}#{anchor}" for path, anchor in ref_items}
    known = metrics["known_refs"]
    unknown = sorted(
        f"{path}#{anchor}"
        for path, anchor in ref_items
        if not _ref_is_known(path, anchor, known)
    )
    anchor_ratio = float(metrics["anchor_ratio"])
    quote_ratio = float(metrics["quote_ratio"])
    score = int(round(anchor_ratio * 60 + quote_ratio * 40))
    if metrics["citations"] and not refs:
        # 有证据但答案没有内联引用：形式分再高也不能算“有据”。
        score = min(score, 40)
    if refs and unknown:
        score = max(0, score - min(30, len(unknown) * 10))
    threshold = max(0, min(100, int(pass_score)))
    supported = (
        bool(metrics["citations"]) and score >= threshold and not unknown
    )
    return {
        "mode": "deterministic",
        "score": score,
        "pass_score": threshold,
        "supported": supported,
        "unsupported_claims": [],
        "citation_issues": unknown[:10],
        "unverified_quotes": metrics["unverified_quotes"][:10],
        "quote_verified_ratio": round(metrics["quote_verified_ratio"], 4),
        "summary": (
            f"证据 {metrics['citations']} 条（原文命中 "
            f"{metrics['quote_verified_ratio']:.0%}）；"
            f"答案引用 {len(refs)} 个锚点，无法对应 {len(unknown)} 个"
        ),
    }


async def judge_document_answer(
    llm: Any | None,
    question: str,
    answer: str,
    sources: list[dict[str, Any]],
    config: RunnableConfig | None = None,
    *,
    pass_score: int = 60,
) -> dict[str, Any]:
    threshold = max(0, min(100, int(pass_score)))
    deterministic = deterministic_judgement(
        answer, sources, pass_score=threshold
    )
    if llm is None:
        return deterministic
    evidence_lines = []
    for item in (sources or [])[:40]:
        if not isinstance(item, dict):
            continue
        evidence_lines.append(
            f"- {item.get('path', '')}#{item.get('anchor', '')} "
            f"{(item.get('claim', '') or '')} "
            f"{(item.get('quote', '') or '')}"[:800]
        )
    body = (
        f"问题：{question}\n\n答案：\n{answer[:12_000]}\n\n"
        "证据：\n" + ("\n".join(evidence_lines) or "（无）")
    )
    try:
        structured = with_json_output(llm, DocumentJudgement)
        result = await structured.ainvoke(
            [
                SystemMessage(content=JUDGE_PROMPT),
                HumanMessage(content=body),
            ],
            config,
        )
        judgement = (
            result
            if isinstance(result, DocumentJudgement)
            else DocumentJudgement.model_validate(result)
        )
    except Exception as exc:  # noqa: BLE001 - judge 失败回退确定性
        deterministic["error"] = f"{type(exc).__name__}: {exc}"
        return deterministic
    payload = judgement.model_dump()
    payload.update(
        {
            "mode": "llm",
            "pass_score": threshold,
            "llm_supported": bool(judgement.supported),
            # 以分数 + 确定性引用问题为准，避免 LLM 宽松放过坏引用。
            "supported": bool(
                judgement.score >= threshold
                and not deterministic["citation_issues"]
            ),
            "deterministic_score": deterministic["score"],
            "deterministic_supported": deterministic["supported"],
            "unverified_quotes": deterministic.get("unverified_quotes", []),
            "quote_verified_ratio": deterministic.get(
                "quote_verified_ratio", 0.0
            ),
        }
    )
    return payload
