"""lint 的**棘轮**（ratchet）：存量不再增长，只许下降。

为什么这样：`ruff` 的 `S`（bandit）与 `C90/TRY/RET/ASYNC/A` 一次开出来是
几百条存量，全修完需要单独的大工程；但直接 `ignore` 掉就等于永远关掉这条
闸门——新代码还能继续往里加 `try-except-pass`、继续堆复杂度。

所以本仓库的做法是：**存量记进预算文件，CI 只卡"不许变多"**。修一处就把
`lint_budget.json` 里那个数字减一（用 `--update` 重生成），数字只降不升，
闸门始终有效。

`pyproject.toml` 里**整条 ignore** 的规则（如 `TRY003`、`ASYNC240`）是已经
逐条评审过、确定"这类写法本身没问题"的，因此不进预算。区分标准很简单：
"这条以后还可能出现，且出现就该被看到" → 进预算；"这条出现也是合理的" →
整条 ignore。

预算只针对 `src/`（产品代码）；`tests/` 不参与。

用法：
    python scripts/check_lint_budget.py            # CI：超预算即失败
    python scripts/check_lint_budget.py --update   # 修完之后重生成预算
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGET_PATH = Path(__file__).resolve().parent / "lint_budget.json"
TARGET = "src"
# 必须与 `pyproject.toml` 的 `BUDGETED_RULES` 一致。
SELECT = "S,C90,TRY,RET,ASYNC,A,T20"
def _counts() -> Counter:
    """统计被预算跟踪的规则（`pyproject.toml` 的 `BUDGETED_RULES`）。

    这些规则**不在 `select` 里**（否则 CI 会立刻变红、然后被 ignore 掉），
    所以这里必须显式 `--select`。配置里的 `ignore` 仍然生效 —— 已评审为
    "这类写法没问题"的 TRY003 / ASYNC240 不会混进预算。
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            SELECT,
            "--output-format",
            "json",
            TARGET,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # ruff 找不到规则/目标时返回非 0，但 json 仍可能有内容；只在完全没输出时报错。
    if not result.stdout.strip():
        raise SystemExit(
            f"ruff 没有产出可解析的结果：{result.stderr.strip()[:400]}"
        )
    payload = json.loads(result.stdout)
    # `--select` 不会让配置里的 `ignore` 生效，所以整条 ignore 的规则
    # （TRY003 / ASYNC240，已评审为"这类写法没问题"）会混进来。按 pyproject
    # 的 `ignore` 过滤掉，否则预算里会躺着一个永远不该修的数字。
    skipped = _ignored_codes()
    counter: Counter = Counter()
    for item in payload:
        code = str(item.get("code") or "").strip()
        if code and code not in skipped:
            counter[code] += 1
    return skipped, counter


def _ignored_codes() -> set[str]:
    """读 `pyproject.toml` 的 `[tool.ruff.lint].ignore`。"""
    import tomllib

    data = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    lint = data.get("tool", {}).get("ruff", {}).get("lint", {})
    return {str(code) for code in (lint.get("ignore") or [])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="按当前代码重生成预算文件（修完之后用；数字只许降不许升）",
    )
    args = parser.parse_args(argv)

    ignored, actual = _counts()

    if args.update:
        BUDGET_PATH.write_text(
            json.dumps(
                dict(sorted(actual.items())), ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"已更新 {BUDGET_PATH.name}：{dict(sorted(actual.items()))}")
        return 0

    try:
        budget = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"无法读取预算文件 {BUDGET_PATH}：{exc}")
        return 1

    if ignored:
        print(f"整条 ignore、不进预算：{', '.join(sorted(ignored))}")

    errors: list[str] = []
    for code, limit in sorted(budget.items()):
        got = int(actual.get(code, 0))
        flag = "OK " if got <= int(limit) else "超 "
        print(f"{flag}{code}: {got} / 预算 {limit}")
        if got > int(limit):
            errors.append(
                f"{code}: {got} > 预算 {limit}（新增的必须先修，不能抬预算）"
            )
    for code in sorted(set(actual) - set(budget)):
        errors.append(f"{code}: 新出现的规则 {actual[code]} 处，预算里没有它")

    if errors:
        print("\n安全 lint 预算超支：")
        for error in errors:
            print(f"  - {error}")
        print(
            "\n修掉新增项，或先修存量再用 `--update` 下调预算；"
            "抬预算等于把闸门关掉。"
        )
        return 1
    print("\n安全 lint 预算未超支。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
