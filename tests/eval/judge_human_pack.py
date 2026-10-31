"""150 条待人工审核的 judge 标注包。

这些是**候选样本**，`label_source=pending-human`、`label=""`，不能直接用于
校准。人工逐条填写 `label`、`reviewer`、`reviewed_at` 后，用
`promote_case` 转成 `label_source=human` 才允许进入标注集。

AI 不能替代真人标注；这里只提供可交接的审核队列与强制字段。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PENDING_SOURCE = "pending-human"
HUMAN_SOURCE = "human"
LABELS = {"pass", "fail"}


def build_pending_pack(target: int = 150) -> list[dict[str, Any]]:
    target = max(1, int(target))
    topics = (
        ("折扣", "pricing", "page=3", "折扣逻辑位于 pricing.py 的 apply_discount"),
        ("退款", "refund", "sheet=Refund", "退款状态由 refund_status 表驱动"),
        ("报表", "reporting", "slide=4", "报表使用 revenue_cents 字段"),
        ("权限", "auth", "section=4.2", "权限检查在 approve.py 的 tool_level"),
        ("缓存", "cache", "page=7", "缓存命中率按 provider 统计"),
        ("限流", "ratelimit", "page=2", "429 后按指数退避冷却"),
        ("索引", "index", "page=9", "架构索引按内容哈希增量更新"),
        ("评测", "eval", "page=1", "judge 阈值由标注集校准"),
    )
    cases: list[dict[str, Any]] = []
    index = 0
    while len(cases) < target:
        name, path, anchor, quote = topics[index % len(topics)]
        round_no = index // len(topics)
        cases.append(
            {
                "id": f"review/{index}",
                "question": f"{name}（样本 {round_no}）是怎么实现的？",
                "answer": (
                    f"{name}见 [来源: {path}.pdf#{anchor}]，"
                    "请审核这条引用与结论是否一致。"
                ),
                "sources": [
                    {
                        "path": f"{path}.pdf",
                        "anchor": anchor,
                        "quote": quote,
                        "claim": f"{name}相关结论",
                    }
                ],
                "label": "",
                "label_source": PENDING_SOURCE,
                "reviewer": "",
                "reviewed_at": "",
            }
        )
        index += 1
    return cases


def promote_case(
    case: dict[str, Any],
    *,
    label: str,
    reviewer: str,
    reviewed_at: str,
) -> dict[str, Any]:
    if str(label) not in LABELS:
        raise ValueError(f"label 必须是 pass/fail：{label!r}")
    if not str(reviewer).strip():
        raise ValueError("缺少 reviewer")
    promoted = dict(case)
    promoted.update(
        {
            "label": str(label),
            "label_source": HUMAN_SOURCE,
            "reviewer": str(reviewer).strip(),
            "reviewed_at": str(reviewed_at).strip(),
        }
    )
    return promoted


def export_pending(path: str | Path, target: int = 150) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in build_pending_pack(target)
        )
        + "\n",
        encoding="utf-8",
    )
