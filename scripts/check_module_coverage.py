"""承重模块覆盖率棘轮：全局平均不许再掩盖安全模块。

`pyproject.toml` 的 `fail_under=76` 是全局门槛；`check_layer_coverage.py`
只在带 WSL2 的自托管 runner 上管沙箱层。本脚本补的是**Linux CI 子集**里
账号、审批、执行契约与路由装配这些承重模块：每个模块有独立下限，数字只许
上升。

用法：
    pytest -m "not wsl and not llm" --cov --cov-report=json:coverage.json
    python scripts/check_module_coverage.py --json coverage.json
    python scripts/check_module_coverage.py --json coverage.json --update
    python scripts/check_module_coverage.py --json coverage.json --strict-target
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGET_PATH = Path(__file__).resolve().parent / "coverage_budget.json"


def _relative(filename: str) -> str:
    text = str(filename).replace("\\", "/")
    marker = "coding_agent/"
    index = text.rfind(marker)
    return text[index + len(marker) :] if index >= 0 else text


def _pct(entry: dict) -> float:
    summary = entry.get("summary") or {}
    statements = int(summary.get("num_statements") or 0)
    if statements == 0:
        return 100.0
    missing = int(summary.get("missing_lines") or 0)
    return (statements - missing) * 100.0 / statements


def _load_budget(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"无法读取覆盖率预算 {path}：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(
        data.get("modules"), dict
    ):
        raise SystemExit(f"覆盖率预算格式非法：{path}")
    return data


def _measure(report: dict, modules: dict[str, float]) -> dict[str, float]:
    files = report.get("files") or {}
    measured: dict[str, float] = {}
    for filename, entry in files.items():
        relative = _relative(str(filename))
        if relative in modules:
            measured[relative] = _pct(entry)
    return measured


def _print_table(
    measured: dict[str, float],
    floors: dict[str, float],
    target: float,
    wsl_gated: set[str],
) -> None:
    for module in sorted(floors):
        actual = measured.get(module)
        if actual is None:
            print(f"{module}: 缺少覆盖率数据（门槛 {floors[module]:.1f}%）")
            continue
        suffix = " (WSL gate)" if module in wsl_gated else ""
        state = "OK" if actual >= floors[module] else "FAIL"
        warn = " · 低于目标" if actual < target else ""
        print(
            f"{state:4} {module}: {actual:.1f}% "
            f"(下限 {floors[module]:.1f}% / 目标 {target:.0f}%)"
            f"{warn}{suffix}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="coverage.json")
    parser.add_argument("--budget", default=str(BUDGET_PATH))
    parser.add_argument(
        "--update",
        action="store_true",
        help="按当前覆盖率抬高下限（只升不降；回归需要 --force）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="允许 --update 下调下限（仅用于确认过的平台差异）",
    )
    parser.add_argument(
        "--strict-target",
        action="store_true",
        help="低于目标 85%% 也失败（夜间/发版用）",
    )
    args = parser.parse_args(argv)

    report_path = Path(args.json)
    if not report_path.is_file():
        print(f"找不到覆盖率报告：{report_path}")
        return 1
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"覆盖率报告无法解析：{exc}")
        return 1

    budget = _load_budget(Path(args.budget))
    floors = {
        str(module): float(value)
        for module, value in (budget.get("modules") or {}).items()
    }
    target = float(budget.get("target") or 85.0)
    wsl_gated = {str(item) for item in (budget.get("wsl_gated") or [])}
    measured = _measure(report, floors)

    if args.update:
        changed: dict[str, float] = {}
        for module, old in sorted(floors.items()):
            actual = measured.get(module)
            if actual is None:
                print(f"警告：{module} 没有覆盖率数据，保留原下限")
                continue
            if actual + 1e-9 < old and not args.force:
                print(
                    f"拒绝下调 {module}: {actual:.1f}% < 现下限 {old:.1f}%"
                    "（确认是平台差异时加 --force）"
                )
                return 1
            # 只抬高，且留 1pp 余量，避免平台噪声导致天天红。
            candidate = max(old, round(actual - 1.0, 1))
            floors[module] = candidate
            if candidate != old:
                changed[module] = candidate
        budget["modules"] = dict(sorted(floors.items()))
        Path(args.budget).write_text(
            json.dumps(budget, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"已更新 {args.budget}：{changed or '无变化'}")
        return 0

    _print_table(measured, floors, target, wsl_gated)
    errors: list[str] = []
    for module, floor in floors.items():
        actual = measured.get(module)
        if actual is None:
            errors.append(f"{module}：覆盖率报告里没有它")
            continue
        if actual < floor:
            errors.append(
                f"{module}：{actual:.1f}% < 下限 {floor:.1f}%"
            )
        elif args.strict_target and actual < target:
            errors.append(
                f"{module}：{actual:.1f}% < 目标 {target:.0f}%"
            )
    if errors:
        print("\n承重模块覆盖率未达标：")
        for error in errors:
            print(f"  - {error}")
        print(
            "\n修完后用 `--update` 抬高预算；不要手改降低。"
        )
        return 1
    print("\n承重模块覆盖率棘轮通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
