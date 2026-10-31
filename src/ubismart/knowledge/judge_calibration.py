"""文档 judge 阈值校准（纯函数）。

输入是标注样本 `[(score, label)]`；输出是使 F1 最大（并列时准确率、再并列取
更高阈值）的通过阈值。只做阈值选择，不训练模型。
"""

from __future__ import annotations

from typing import Any

DEFAULT_THRESHOLD = 60


def _label(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "pass", "passed", "yes", "ok"}:
        return True
    if text in {"0", "false", "fail", "failed", "no", "bad"}:
        return False
    return None


def calibrate_threshold(
    samples: list[dict[str, Any]],
    *,
    min_score: int = 0,
    max_score: int = 100,
    default: int = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    rows: list[tuple[int, bool]] = []
    skipped = 0
    for item in samples:
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            score = max(0, min(100, int(item.get("score", 0) or 0)))
        except (TypeError, ValueError):
            skipped += 1
            continue
        label = _label(item.get("label"))
        if label is None:
            skipped += 1
            continue
        rows.append((score, label))
    if not rows:
        return {
            "threshold": max(0, min(100, int(default))),
            "samples": 0,
            "skipped": skipped,
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
        }

    best: dict[str, Any] | None = None
    low = max(0, int(min_score))
    high = max(low, min(100, int(max_score)))
    for threshold in range(low, high + 2):
        true_positive = false_positive = true_negative = false_negative = 0
        for score, label in rows:
            predicted = score >= threshold
            if predicted and label:
                true_positive += 1
            elif predicted and not label:
                false_positive += 1
            elif not predicted and label:
                false_negative += 1
            else:
                true_negative += 1
        total = len(rows)
        accuracy = (true_positive + true_negative) / total
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        candidate = {
            "threshold": threshold,
            "samples": total,
            "skipped": skipped,
            "accuracy": round(accuracy, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "true_positive": true_positive,
            "false_positive": false_positive,
            "true_negative": true_negative,
            "false_negative": false_negative,
        }
        key = (f1, accuracy, threshold)
        if best is None or key > (
            best["f1"],
            best["accuracy"],
            best["threshold"],
        ):
            best = candidate
    return best or {
        "threshold": default,
        "samples": 0,
        "skipped": skipped,
    }
