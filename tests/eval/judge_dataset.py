"""文档 judge 的种子标注集（可扩展）。

这是**可复现的种子集**，不是统计意义上的大规模人工标注：8 个主题 ×
（有证据 / 错锚点 / 无证据）三类，共 24 条。新增真实标注时按同样字段追加，
再用 `scripts/calibrate_judge.py` 重新校准阈值。

字段：`{id, question, answer, sources, label}`，label ∈ pass/fail。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["JUDGE_CASES", "as_jsonl", "write_jsonl"]


_TOPICS: tuple[tuple[str, str, str, str], ...] = (
    ("折扣", "pricing", "page=3", "折扣逻辑位于 pricing.py 的 apply_discount"),
    ("退款", "refund", "sheet=Refund", "退款状态由 refund_status 表驱动"),
    ("报表", "reporting", "slide=4", "报表使用 revenue_cents 字段"),
    ("权限", "auth", "section=4.2", "权限检查在 approve.py 的 tool_level"),
    ("缓存", "cache", "page=7", "缓存命中率按 provider 统计"),
    ("限流", "ratelimit", "page=2", "429 后按指数退避冷却"),
    ("索引", "index", "page=9", "架构索引按内容哈希增量更新"),
    ("评测", "eval", "page=1", "judge 阈值由标注集校准"),
)


def _cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for index, (name, path, anchor, quote) in enumerate(_TOPICS):
        sources = [
            {
                "path": f"{path}.pdf",
                "anchor": anchor,
                "quote": quote,
                "claim": f"{name}相关结论",
            }
        ]
        cases.append(
            {
                "id": f"seed/{index}/pass",
                "question": f"{name}是怎么实现的？",
                "answer": f"{name}的实现见 [来源: {path}.pdf#{anchor}]。",
                "sources": sources,
                "label": "pass",
            }
        )
        cases.append(
            {
                "id": f"seed/{index}/unknown-anchor",
                "question": f"{name}是怎么实现的？",
                "answer": (
                    f"{name}的实现见 [来源: {path}.pdf#page=999]"
                    "（该锚点不在证据里）。"
                ),
                "sources": sources,
                "label": "fail",
            }
        )
        cases.append(
            {
                "id": f"seed/{index}/no-evidence",
                "question": f"{name}是怎么实现的？",
                "answer": f"{name}由内部模块自动完成，没有引用。",
                "sources": [],
                "label": "fail",
            }
        )
    return cases


JUDGE_CASES: tuple[dict[str, Any], ...] = tuple(_cases())


def as_jsonl() -> str:
    return "\n".join(
        json.dumps(item, ensure_ascii=False) for item in JUDGE_CASES
    )


def write_jsonl(path: str | Path) -> None:
    Path(path).write_text(as_jsonl() + "\n", encoding="utf-8")
