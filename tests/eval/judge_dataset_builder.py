"""judge 标注集的构建、校验、去重与切分。

⚠️ 生成数据是**合成标注**（`label_source=synthetic`），用于跑通大规模校准
流水线，不替代真实人工标注。真实标注只需按同一 schema 追加，并把
`label_source` 设为 `human`；`validate_cases(require_human=True)` 可强制检查。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

VARIANTS = ("pass", "unknown-anchor", "no-evidence", "no-anchor")
LABELS = {"pass", "fail"}


def _topics(count: int) -> list[tuple[str, str, str, str]]:
    names = (
        "折扣", "退款", "报表", "权限", "缓存", "限流", "索引", "评测",
        "审批", "沙箱", "记忆", "恢复", "并发", "预算", "检索", "架构",
        "文档", "图像", "报告", "测试",
    )
    topics: list[tuple[str, str, str, str]] = []
    index = 0
    while len(topics) < count:
        name = f"{names[index % len(names)]}-{index // len(names)}"
        path = f"doc-{index}"
        topics.append(
            (
                name,
                path,
                f"page={index % 50 + 1}",
                f"{name}的实现细节与证据原文",
            )
        )
        index += 1
    return topics


def build_cases(target: int = 1_000) -> list[dict[str, Any]]:
    target = max(1, int(target))
    per_topic = len(VARIANTS)
    topics = _topics(max(1, (target + per_topic - 1) // per_topic))
    cases: list[dict[str, Any]] = []
    for topic_index, (name, path, anchor, quote) in enumerate(topics):
        sources = [
            {
                "path": f"{path}.pdf",
                "anchor": anchor,
                "quote": quote,
                "claim": f"{name}相关结论",
            }
        ]
        variants: dict[str, dict[str, Any]] = {
            "pass": {
                "answer": f"{name}见 [来源: {path}.pdf#{anchor}]。",
                "sources": sources,
                "label": "pass",
            },
            "unknown-anchor": {
                "answer": f"{name}见 [来源: {path}.pdf#page=999]。",
                "sources": sources,
                "label": "fail",
            },
            "no-evidence": {
                "answer": f"{name}由内部模块自动完成，没有引用。",
                "sources": [],
                "label": "fail",
            },
            "no-anchor": {
                "answer": f"{name}的结论来自未标注材料。",
                "sources": [{"path": f"{path}.pdf", "quote": quote}],
                "label": "fail",
            },
        }
        for variant in VARIANTS:
            payload = variants[variant]
            cases.append(
                {
                    "id": f"gen/{topic_index}/{variant}",
                    "question": f"{name}是怎么实现的？",
                    "answer": payload["answer"],
                    "sources": payload["sources"],
                    "label": payload["label"],
                    "label_source": "synthetic",
                }
            )
            if len(cases) >= target:
                return cases
    return cases


def validate_cases(
    cases: Iterable[dict[str, Any]], *, require_human: bool = False
) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(cases):
        if not isinstance(item, dict):
            errors.append(f"#{index}: 不是对象")
            continue
        case_id = str(item.get("id", ""))
        if not case_id:
            errors.append(f"#{index}: 缺少 id")
        elif case_id in seen:
            errors.append(f"{case_id}: id 重复")
        seen.add(case_id)
        if not str(item.get("question", "")).strip():
            errors.append(f"{case_id}: 缺少 question")
        if not str(item.get("answer", "")).strip():
            errors.append(f"{case_id}: 缺少 answer")
        if str(item.get("label", "")) not in LABELS:
            errors.append(f"{case_id}: label 非法")
        if not isinstance(item.get("sources"), list):
            errors.append(f"{case_id}: sources 不是数组")
        if require_human and item.get("label_source") != "human":
            errors.append(f"{case_id}: 不是人工标注")
    return errors


def dedupe_cases(
    cases: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in cases:
        key = hashlib.sha1(
            (
                str(item.get("question", ""))
                + "\n"
                + str(item.get("answer", ""))
                + "\n"
                + json.dumps(item.get("sources") or [], sort_keys=True)
            ).encode("utf-8")
        ).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def split_cases(
    cases: list[dict[str, Any]],
    *,
    train: float = 0.7,
    calibration: float = 0.15,
) -> dict[str, list[dict[str, Any]]]:
    ordered = sorted(
        cases, key=lambda item: hashlib.sha1(
            str(item.get("id", "")).encode("utf-8")
        ).hexdigest()
    )
    total = len(ordered)
    train_end = int(total * max(0.0, min(1.0, train)))
    cal_end = train_end + int(
        total * max(0.0, min(1.0, calibration))
    )
    return {
        "train": ordered[:train_end],
        "calibration": ordered[train_end:cal_end],
        "test": ordered[cal_end:],
    }


def balance_stats(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    labels = Counter(str(item.get("label", "")) for item in cases)
    sources = Counter(str(item.get("label_source", "")) for item in cases)
    return {
        "total": sum(labels.values()),
        "labels": dict(labels),
        "label_sources": dict(sources),
    }


def import_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def export_jsonl(
    cases: Iterable[dict[str, Any]], path: str | Path
) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False) for item in cases
        )
        + "\n",
        encoding="utf-8",
    )
