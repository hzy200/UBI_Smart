"""把评测结果读成**结论**，而不是丢一个 JSON 给人自己看。

背景（TD-006）：`tests/eval` 不在 PR 门禁里，套件什么时候饱和的没人知道 ——
等下次想量化改动时才发现尺子已经废了。这里补的是"读结果"这一步：

- **饱和**：某个套件所有档位都 100%，说明它已经失去区分度。这不是代码坏了，
  所以只报警告（退出码 0），但必须出现在 CI 摘要里，不能静默。
- **退化**：与上一份可比基线相比通过率下降超过阈值 —— 这是真问题，退出码 1。
- **不可比**：`comparable=false`（子集运行、工作区脏、沙箱缺 pytest）时，
  任何结论都不成立，只报告不判定。

用法：
    python scripts/eval_gate.py llm-eval-123.json
    python scripts/eval_gate.py new.json --compare tests/eval/baseline_long.json
    python scripts/eval_gate.py new.json --markdown
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 少于这个任务数就不谈"饱和"：3 个任务全过说明不了尺子有没有区分度。
MIN_TASKS_FOR_SATURATION = 10


@dataclass(frozen=True)
class Verdict:
    """一份评测结果的结论。字段都直接进摘要，不要塞过程数据。"""

    suite: str
    total: int
    passed: int
    pass_rate: float
    comparable: bool
    comparable_reason: str = ""
    saturated: bool = False
    saturated_tiers: list[str] = field(default_factory=list)
    regression: float = 0.0  # 正数表示下降了多少
    previous_rate: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """只有"可比且退化"才算失败。

        饱和与不可比都是"这次读数没有意义"，不是"代码坏了" —— 用退出码区分
        这两件事，CI 才不会因为尺子失效而变红（那会让人习惯性忽略红灯）。
        """
        return self.comparable and self.regression > 0


def _rate(stats: dict[str, Any]) -> float:
    total = int(stats.get("total") or 0)
    return (int(stats.get("passed") or 0) / total) if total else 0.0


def judge(
    summary: dict[str, Any],
    *,
    previous: dict[str, Any] | None = None,
    threshold: float = 0.10,
    min_tasks: int = MIN_TASKS_FOR_SATURATION,
) -> Verdict:
    """把 runner 产出的 summary 读成结论。纯函数，便于单测。"""
    total = int(summary.get("total") or 0)
    passed = int(summary.get("passed") or 0)
    rate = float(summary.get("pass_rate") or 0.0)
    tiers: dict[str, Any] = summary.get("by_tier") or {}
    saturated_tiers = [
        str(name) for name, stats in tiers.items() if _rate(stats) >= 1.0
    ]
    # 全部档位都满、且任务数够 —— 才算饱和。单个档位 100% 很正常。
    saturated = (
        bool(tiers) and len(saturated_tiers) == len(tiers) and total >= min_tasks
    )

    regression = 0.0
    previous_rate: float | None = None
    notes: list[str] = []
    if previous is not None:
        previous_rate = float(previous.get("pass_rate") or 0.0)
        drop = previous_rate - rate
        if drop > threshold:
            regression = drop
        elif drop > 0:
            notes.append(
                f"通过率较上次下降 {drop:.0%}（在 {threshold:.0%} 阈值内）"
            )

    verdict = Verdict(
        suite=str(summary.get("suite") or "unknown"),
        total=total,
        passed=passed,
        pass_rate=rate,
        comparable=bool(summary.get("comparable", True)),
        comparable_reason=str(summary.get("comparable_reason") or ""),
        saturated=saturated,
        saturated_tiers=sorted(saturated_tiers),
        regression=regression,
        previous_rate=previous_rate,
        notes=notes,
    )

    if not verdict.comparable:
        verdict.notes.append(
            "本次结果不可比（"
            + (verdict.comparable_reason or "未说明原因")
            + "）：只能看趋势，不能据此判定改进或退化"
        )
    if verdict.saturated:
        verdict.notes.append(
            f"套件已饱和（{', '.join(verdict.saturated_tiers)} 全部 100%，"
            f"{total} 个任务）：它已失去区分度，继续做 A/B 只会得到 0pp"
        )
    if total < min_tasks:
        verdict.notes.append(
            (
                f"任务数 {total} < {min_tasks}：单轮分辨率约 {1 / total:.1%}，"
                "比它更小的差异无法与噪声区分"
            )
            if total
            else "没有任务，无法判定"
        )
    return verdict


def render(verdict: Verdict, *, markdown: bool = True) -> str:
    marker = "❌" if verdict.failed else ("⚠️" if verdict.saturated else "✅")
    if markdown:
        lines = [f"### {marker} 评测结论：`{verdict.suite}`", ""]
    else:
        lines = [f"{marker} 评测结论：{verdict.suite}"]

    delta = ""
    if verdict.previous_rate is not None:
        delta = f"（上次 {verdict.previous_rate:.0%}）"
    lines.append(
        f"- 通过率：{verdict.passed}/{verdict.total} = "
        f"{verdict.pass_rate:.0%}{delta}"
    )
    if verdict.saturated_tiers:
        lines.append(f"- 已饱和档位：{', '.join(verdict.saturated_tiers)}")
    if verdict.regression > 0:
        lines.append(f"- **退化 {verdict.regression:.0%}，超过阈值**")
    for note in verdict.notes:
        lines.append(f"- {note}")
    return "\n".join(lines)


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 不是评测结果对象")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把评测结果读成结论")
    parser.add_argument("result", type=Path, help="本次评测输出 JSON")
    parser.add_argument("--compare", type=Path, help="上一份基线 JSON")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.10,
        help="通过率下降超过这个值判为退化（默认 10%%）",
    )
    parser.add_argument("--min-tasks", type=int, default=MIN_TASKS_FOR_SATURATION)
    parser.add_argument(
        "--markdown", action="store_true", help="输出 GitHub Step Summary 用的 markdown"
    )
    args = parser.parse_args(argv)

    try:
        summary = _load(args.result)
    except (OSError, ValueError) as exc:
        print(f"读取评测结果失败：{exc}", file=sys.stderr)
        return 2
    previous = None
    if args.compare is not None:
        try:
            previous = _load(args.compare)
        except (OSError, ValueError) as exc:
            print(f"读取对比基线失败：{exc}", file=sys.stderr)
            return 2

    verdict = judge(
        summary, previous=previous, threshold=args.threshold, min_tasks=args.min_tasks
    )
    print(render(verdict, markdown=args.markdown))
    if verdict.failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
