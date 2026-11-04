"""从文献矩阵提炼可证伪的创新候选，并用 critic 复核。

只做“候选生成 + 证据归属/可证伪性检查”，**不宣称新颖性已被证明**。
输入是 `DocumentMatrix`（每篇固定槽位）与已验证 sources；输出候选必须带
原文证据，critic 只能依据给定材料判断，不能使用外部知识。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import AliasChoices, BaseModel, Field

from ubismart.llm.deepseek import with_json_output

_SLOTS = (
    ("problem", "问题/动机"),
    ("method", "方法"),
    ("algorithm", "算法/公式"),
    ("experiments", "实验/数字"),
    ("limitations", "局限"),
    ("innovations", "论文自身创新"),
    ("conclusion", "结论"),
)
_REQUIRED = (
    "title",
    "gap",
    "insight",
    "hypothesis",
    "novelty_claim",
    "prior_art_contrast",
    "falsifiable_experiment",
)
_CONFIDENCE = {"low", "medium", "high"}


class InnovationEvidence(BaseModel):
    path: str = Field(
        default="",
        validation_alias=AliasChoices(
            "path", "evidence_path", "file", "source"
        ),
    )
    anchor: str = Field(
        default="",
        validation_alias=AliasChoices("anchor", "page", "location"),
    )
    claim: str = Field(
        default="", validation_alias=AliasChoices("claim", "text")
    )
    quote: str = ""


class InnovationCandidate(BaseModel):
    title: str = ""
    gap: str = ""
    insight: str = ""
    hypothesis: str = ""
    novelty_claim: str = ""
    prior_art_contrast: str = ""
    falsifiable_experiment: str = ""
    risks: str = ""
    confidence: str = "medium"
    evidence: list[InnovationEvidence] = Field(default_factory=list)


class InnovationSet(BaseModel):
    candidates: list[InnovationCandidate] = Field(default_factory=list)


class NoveltyReview(BaseModel):
    title: str = ""
    supported: bool = False
    score: int = 0
    novelty: str = "low"
    issues: list[str] = Field(default_factory=list)


class NoveltyReviewSet(BaseModel):
    reviews: list[NoveltyReview] = Field(default_factory=list)


INNOVATION_PROMPT = """\
你是文献创新点提炼 worker。只依据给定的文档矩阵与证据，提出可证伪的
创新候选；不要复述论文自己的贡献当作“新创新”。候选应尽量覆盖至少两篇
不同文档，单一文档不要占超过一半候选。
每个候选包含：
- gap：现有材料暴露的空白/矛盾/局限；
- insight：为什么这个空白值得补；
- hypothesis：可检验的假设；
- novelty_claim：相对**给定材料**的新意（不要声称外部文献没有做过）；
- prior_art_contrast：与给定论文已有做法的差异；
- falsifiable_experiment：能在小规模数据/玩具任务上验证或推翻的实验；
- risks：主要失败模式；
- confidence：low/medium/high；
- evidence：只能复制给定证据中的 path/anchor/claim/quote，至少 1 条。
输出 JSON：
{"candidates":[{"title":"...","gap":"...","insight":"...","hypothesis":"...",
"novelty_claim":"...","prior_art_contrast":"...","falsifiable_experiment":"...",
"risks":"...","confidence":"medium","evidence":[{"path":"...","anchor":"...",
"claim":"...","quote":"..."}]}]}
"""

NOVELTY_PROMPT = """\
你是创新点 critic。只依据候选自身字段与给定证据复核，不使用外部知识。
逐条判断：
- 证据是否真的支持 gap/hypothesis；
- novelty_claim 是否只是复述某篇论文已有贡献；
- falsifiable_experiment 是否给出可观测指标/对照/失败条件；
- 是否依赖给定材料之外的前提。
输出 JSON：{"reviews":[{"title":"...","supported":true/false,"score":0-100,
"novelty":"low|medium|high","issues":["..."]}]}
"""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _render_matrix(matrix: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for entry in matrix or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", "")).strip()
        title = str(entry.get("title", "")).strip()
        lines = [f"## {path}" + (f" | {title}" if title else "")]
        for key, label in _SLOTS:
            value = _normalize(entry.get(key, ""))
            if value:
                lines.append(f"{label}：{value[:500]}")
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _render_sources(
    sources: list[dict[str, Any]], limit: int = 60
) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        path = str(source.get("path", "")).strip()
        anchor = str(source.get("anchor", "")).strip()
        ref = f"{path}#{anchor}"
        if not path or not anchor or ref in seen:
            continue
        seen.add(ref)
        lines.append(
            f"- {str(source.get('claim', ''))[:300]} "
            f"[{ref}] {str(source.get('quote', ''))[:300]}"
        )
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def _parse(
    payload: Any, schema: type
) -> tuple[Any | None, int, int]:
    if isinstance(payload, schema):
        return payload, 0, 0
    if not isinstance(payload, dict):
        return None, 0, 0
    used_in = used_out = 0
    raw = payload.get("raw")
    if raw is not None:
        meta = getattr(raw, "usage_metadata", None) or {}
        used_in = int(meta.get("input_tokens") or 0)
        used_out = int(meta.get("output_tokens") or 0)
    parsed = payload.get("parsed")
    if isinstance(parsed, schema):
        return parsed, used_in, used_out
    if isinstance(parsed, dict):
        try:
            return schema.model_validate(parsed), used_in, used_out
        except Exception:  # noqa: BLE001 - 解析失败按空结果处理
            return None, used_in, used_out
    return None, used_in, used_out


def _source_refs(
    sources: list[dict[str, Any]],
) -> tuple[set[str], dict[str, str], dict[str, str]]:
    paths: set[str] = set()
    refs: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        path = str(source.get("path", "")).strip()
        anchor = str(source.get("anchor", "")).strip()
        if not path:
            continue
        paths.add(path)
        by_name.setdefault(path.rsplit("/", 1)[-1], path)
        if anchor:
            refs[f"{path}#{anchor}"] = path
    return paths, refs, by_name


def _alias(text: str) -> str:
    return re.sub(
        r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").lower()
    )


def _title_aliases(matrix: list[dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for entry in matrix:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", "")).strip()
        title = str(entry.get("title", "")).strip()
        if not path or not title:
            continue
        for value in (title, title.split(":", 1)[0]):
            key = _alias(value)
            if len(key) >= 3:
                aliases.setdefault(key, path)
    return aliases


def _resolve_evidence_path(
    path: str,
    paths: set[str],
    by_name: dict[str, str],
    aliases: dict[str, str],
) -> str:
    candidate = str(path or "").strip()
    if candidate in paths:
        return candidate
    name = candidate.rsplit("/", 1)[-1]
    if name in by_name:
        return by_name[name]
    stem = name.rsplit(".", 1)[0]
    matches = {
        value
        for key, value in by_name.items()
        if key.rsplit(".", 1)[0] == stem
    }
    if len(matches) == 1:
        return next(iter(matches))
    key = _alias(candidate)
    if key in aliases:
        return aliases[key]
    contained = {
        value for alias, value in aliases.items() if alias in key
    }
    return next(iter(contained)) if len(contained) == 1 else ""


def _candidate_valid(candidate: InnovationCandidate) -> tuple[bool, list[str]]:
    issues: list[str] = []
    for key in _REQUIRED:
        if not _normalize(getattr(candidate, key, "")):
            issues.append(f"缺少 {key}")
    if candidate.confidence not in _CONFIDENCE:
        issues.append(f"confidence 非法：{candidate.confidence!r}")
    return not issues, issues


async def extract_innovations(
    llm: Any,
    matrix: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    config: RunnableConfig | None = None,
    *,
    top_k: int = 5,
    min_score: int = 60,
    min_docs: int = 2,
) -> dict[str, Any]:
    """生成创新候选 + critic 复核；返回候选、评分、token 与问题列表。"""
    result: dict[str, Any] = {
        "candidates": [],
        "reviews": [],
        "issues": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }
    matrix_text = _render_matrix(matrix)
    evidence_text = _render_sources(sources)
    if not matrix_text or not evidence_text:
        result["issues"].append("缺少矩阵或证据，无法提炼创新点")
        return result

    body = (
        "## 文档矩阵\n"
        f"{matrix_text}\n\n"
        "## 可用证据\n"
        f"{evidence_text}"
    )
    try:
        structured = with_json_output(
            llm, InnovationSet, include_raw=True
        )
        payload = await structured.ainvoke(
            [
                SystemMessage(content=INNOVATION_PROMPT),
                HumanMessage(content=body),
            ],
            config,
        )
        parsed, used_in, used_out = _parse(payload, InnovationSet)
        result["input_tokens"] += used_in
        result["output_tokens"] += used_out
    except Exception as exc:  # noqa: BLE001 - 失败返回空候选，不编造
        result["issues"].append(
            f"创新点生成失败：{type(exc).__name__}: {exc}"
        )
        return result
    if parsed is None:
        result["issues"].append("创新点输出无法解析")
        return result

    paths, refs, by_name = _source_refs(sources)
    aliases = _title_aliases(matrix)
    valid: list[InnovationCandidate] = []
    seen: set[str] = set()

    def _collect(items: InnovationSet) -> None:
        for candidate in items.candidates:
            ok, issues = _candidate_valid(candidate)
            key = re.sub(
                r"[^0-9a-z\u4e00-\u9fff]+", "",
                _normalize(candidate.novelty_claim).lower(),
            ) or _normalize(candidate.title)
            if not ok:
                result["issues"].append(
                    f"{candidate.title or '未命名'}：{'；'.join(issues)}"
                )
                continue
            resolved: list[InnovationEvidence] = []
            for item in candidate.evidence:
                path = _resolve_evidence_path(
                    item.path, paths, by_name, aliases
                )
                if not path:
                    continue
                item.path = path
                anchor = str(item.anchor).strip()
                if anchor and f"{path}#{anchor}" not in refs:
                    matches = [
                        ref.split("#", 1)[1]
                        for ref in refs
                        if ref.startswith(path + "#")
                        and (
                            anchor in ref.split("#", 1)[1]
                            or ref.split("#", 1)[1] in anchor
                        )
                    ]
                    item.anchor = matches[0] if len(matches) == 1 else ""
                resolved.append(item)
            candidate.evidence = resolved
            if not candidate.evidence:
                result["issues"].append(
                    f"{candidate.title or '未命名'}：没有可归属的证据"
                )
                continue
            if key in seen:
                result["issues"].append(
                    f"{candidate.title or '未命名'}：重复候选"
                )
                continue
            seen.add(key)
            valid.append(candidate)

    _collect(parsed)

    # 首轮候选可能只覆盖一篇长文档；对未覆盖文档做一次有界补充。
    matrix_paths = [
        str(entry.get("path", "")).strip()
        for entry in matrix
        if isinstance(entry, dict) and str(entry.get("path", "")).strip()
    ]
    covered = {
        item.path
        for candidate in valid
        for item in candidate.evidence
        if item.path
    }
    missing = [path for path in matrix_paths if path not in covered]
    if len(covered) < max(1, int(min_docs)) and missing:
        missing_set = set(missing)
        missing_matrix = [
            entry
            for entry in matrix
            if str(entry.get("path", "")).strip() in missing_set
        ]
        missing_sources = [
            source
            for source in sources
            if str(source.get("path", "")).strip() in missing_set
        ]
        supplement = (
            "## 补充要求\n"
            f"已有候选覆盖文档：{'、'.join(sorted(covered)) or '无'}。\n"
            "请只针对以下文档补充 1–2 个候选，不要重复已有标题：\n"
            + "\n".join(f"- {path}" for path in missing)
            + "\n\n## 文档矩阵\n"
            + _render_matrix(missing_matrix)
            + "\n\n## 可用证据\n"
            + _render_sources(missing_sources)
        )
        try:
            payload = await structured.ainvoke(
                [
                    SystemMessage(content=INNOVATION_PROMPT),
                    HumanMessage(content=supplement),
                ],
                config,
            )
            parsed_missing, used_in, used_out = _parse(
                payload, InnovationSet
            )
            result["input_tokens"] += used_in
            result["output_tokens"] += used_out
            if parsed_missing is not None:
                _collect(parsed_missing)
        except Exception as exc:  # noqa: BLE001 - 补充失败不否定首轮
            result["issues"].append(
                f"补充候选失败：{type(exc).__name__}: {exc}"
            )

    if not valid:
        result["issues"].append("没有通过结构/证据校验的候选")
        return result

    # critic：只依据候选与证据；失败时保守地判为未支持。
    review_map: dict[str, NoveltyReview] = {}
    try:
        critic = with_json_output(llm, NoveltyReviewSet, include_raw=True)
        review_payload = await critic.ainvoke(
            [
                SystemMessage(content=NOVELTY_PROMPT),
                HumanMessage(
                    content=json.dumps(
                        {
                            "candidates": [
                                item.model_dump() for item in valid
                            ],
                            "evidence": evidence_text,
                        },
                        ensure_ascii=False,
                    )
                ),
            ],
            config,
        )
        reviews, used_in, used_out = _parse(
            review_payload, NoveltyReviewSet
        )
        result["input_tokens"] += used_in
        result["output_tokens"] += used_out
        if reviews is not None:
            review_map = {
                _normalize(item.title): item for item in reviews.reviews
            }
            result["reviews"] = [
                item.model_dump() for item in reviews.reviews
            ]
    except Exception as exc:  # noqa: BLE001 - critic 失败不编造分数
        result["issues"].append(
            f"novelty critic 失败：{type(exc).__name__}: {exc}"
        )

    output: list[dict[str, Any]] = []
    for candidate in valid:
        review = review_map.get(_normalize(candidate.title))
        supported = bool(
            review
            and review.supported
            and review.score >= int(min_score)
        )
        output.append(
            {
                **candidate.model_dump(),
                "review": review.model_dump() if review else {},
                "supported": supported,
                "evidence_docs": sorted(
                    {item.path for item in candidate.evidence if item.path}
                ),
            }
        )
    output.sort(
        key=lambda item: int((item.get("review") or {}).get("score") or 0),
        reverse=True,
    )
    result["candidates"] = output[: max(1, int(top_k))]
    return result
