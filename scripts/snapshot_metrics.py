"""从覆盖率报告生成指标快照，取代文档里手抄的测试数/覆盖率。

产物：
    docs/METRICS.md    人读（带生成说明与承重模块表）
    docs/metrics.json  机读（CI 漂移校验用）

用法：
    pytest -m "not wsl and not wsl_env and not llm" --cov --cov-report=json:coverage.json
    python scripts/snapshot_metrics.py --coverage coverage.json --collect --write
    python scripts/snapshot_metrics.py --coverage coverage.json --collect --check

`--check` 不比较 Markdown 文本，而是比较 JSON 数字：测试数必须一致，
覆盖率允许平台漂移（总覆盖率 1pp、模块 2pp），避免 Windows/Linux 的
微小差异把门禁变成噪声。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_module_coverage import (  # noqa: E402
    BUDGET_PATH,
    _load_budget,
    _pct,
    _relative,
)

METRICS_MD = ROOT / "docs" / "METRICS.md"
METRICS_JSON = ROOT / "docs" / "metrics.json"
# Windows 本地与 Linux CI 的覆盖率会有平台差异；容差只吸收噪声，
# 不吸收真实回退（模块回退由 coverage_budget.json 的棘轮单独卡）。
TOTAL_TOLERANCE = 2.0
MODULE_TOLERANCE = 3.0


def _collect_tests() -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-n",
            "0",
            "-m",
            "not wsl and not wsl_env and not llm",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and not result.stdout:
        raise SystemExit(
            "pytest --collect-only 失败：" + result.stderr.strip()[:400]
        )
    node_ids = sum(
        1 for line in result.stdout.splitlines() if "::" in line
    )
    if node_ids:
        return node_ids
    # pytest 9 + `-q` 的 collect-only 只按文件输出计数：`path.py: N`。
    return sum(
        int(match.group(1))
        for match in re.finditer(
            r"^\S+\.py:\s+(\d+)\s*$", result.stdout, re.MULTILINE
        )
    )


def _measure(coverage_path: Path, budget: dict) -> dict:
    try:
        report = json.loads(coverage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"无法读取覆盖率报告：{exc}") from exc
    totals = report.get("totals") or {}
    statements = int(totals.get("num_statements") or 0)
    missing = int(totals.get("missing_lines") or 0)
    percent = (
        (statements - missing) * 100.0 / statements
        if statements
        else 100.0
    )
    floors = {
        str(module): float(value)
        for module, value in (budget.get("modules") or {}).items()
    }
    files = report.get("files") or {}
    modules: dict[str, float] = {}
    for filename, entry in files.items():
        relative = _relative(str(filename))
        if relative in floors:
            modules[relative] = _pct(entry)
    return {
        "statements": statements,
        "missing": missing,
        "percent": round(percent, 2),
        "modules": {key: round(value, 2) for key, value in modules.items()},
    }


def _render(
    tests: int,
    coverage: dict,
    budget: dict,
) -> tuple[str, dict]:
    floors = {
        str(module): float(value)
        for module, value in (budget.get("modules") or {}).items()
    }
    target = float(budget.get("target") or 85.0)
    wsl_gated = [str(item) for item in (budget.get("wsl_gated") or [])]
    payload = {
        "schema": 1,
        "tests": int(tests),
        "coverage": coverage,
        "target": target,
        "modules": {
            module: {
                "actual": coverage["modules"].get(module),
                "floor": floor,
            }
            for module, floor in sorted(floors.items())
        },
        "wsl_gated": wsl_gated,
    }
    lines = [
        "---",
        "layer: L3",
        "doc_id: metrics",
        "title: 指标快照（生成）",
        "owner: 项目作者",
        "status: active",
        "last_reviewed: 2026-10-28",
        "review_cadence: 由 CI 覆盖率任务生成；不要手改",
        "---",
        "",
        "# 指标快照（生成）",
        "",
        "> 由 `scripts/snapshot_metrics.py` 从覆盖率报告与 `pytest --collect-only`",
        "> 生成。**不要手改**；重跑命令：",
        ">",
        "> ```bash",
        '> pytest -m "not wsl and not wsl_env and not llm" --cov --cov-report=json:coverage.json',
        "> python scripts/snapshot_metrics.py --coverage coverage.json --collect --write",
        "> ```",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 快反馈用例 | **{tests:,}** |",
        f"| 覆盖率 | **{coverage['percent']:.2f}%** |",
        f"| 总语句 | **{coverage['statements']:,}** |",
        f"| 未覆盖 | **{coverage['missing']:,}** |",
        f"| 承重模块目标 | **{target:.0f}%** |",
        "",
        "## 承重模块",
        "",
        "| 模块 | 当前 | 棘轮下限 | 目标 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for module, floor in sorted(floors.items()):
        actual = coverage["modules"].get(module)
        actual_text = "—" if actual is None else f"{actual:.1f}%"
        lines.append(
            f"| `{module}` | {actual_text} | {floor:.1f}% | {target:.0f}% |"
        )
    if wsl_gated:
        lines.extend(
            [
                "",
                "WSL 门禁（`.github/workflows/sandbox.yml`）另设下限：",
                "、".join(f"`{item}`" for item in wsl_gated) + "。",
            ]
        )
    lines.extend(
        [
            "",
            "> 历史数字不再手抄；以本文件与 CI 输出为准。",
            "",
        ]
    )
    return "\n".join(lines), payload


def _check(
    tests: int,
    coverage: dict,
    budget: dict,
) -> list[str]:
    if not METRICS_JSON.is_file():
        return [f"缺少生成的指标文件：{METRICS_JSON}"]
    try:
        saved = json.loads(METRICS_JSON.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"指标文件不是合法 JSON：{exc}"]
    errors: list[str] = []
    if int(saved.get("tests") or 0) != int(tests):
        errors.append(
            f"测试数漂移：文件 {saved.get('tests')} / 实测 {tests}"
        )
    old_coverage = saved.get("coverage") or {}
    old_percent = float(old_coverage.get("percent") or 0)
    delta = abs(old_percent - float(coverage["percent"]))
    if delta > TOTAL_TOLERANCE:
        errors.append(
            f"总覆盖率漂移 {delta:.2f}pp > {TOTAL_TOLERANCE:.0f}pp："
            f"文件 {old_percent:.2f}% / 实测 {coverage['percent']:.2f}%"
        )
    old_modules = {
        str(module): float((value or {}).get("actual") or 0)
        for module, value in (saved.get("modules") or {}).items()
        if isinstance(value, dict)
    }
    for module, actual in coverage["modules"].items():
        old = old_modules.get(module)
        if old is None:
            errors.append(f"{module}：指标文件缺少该模块")
            continue
        if abs(old - actual) > MODULE_TOLERANCE:
            errors.append(
                f"{module} 漂移 {abs(old - actual):.1f}pp > "
                f"{MODULE_TOLERANCE:.0f}pp：文件 {old:.1f}% / 实测 {actual:.1f}%"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", default="coverage.json")
    parser.add_argument("--budget", default=str(BUDGET_PATH))
    parser.add_argument("--tests", type=int, default=0)
    parser.add_argument(
        "--collect",
        action="store_true",
        help="运行 pytest --collect-only 统计快反馈用例数",
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    tests = int(args.tests or 0)
    if args.collect:
        tests = _collect_tests()
    if tests <= 0:
        if METRICS_JSON.is_file():
            try:
                tests = int(
                    json.loads(
                        METRICS_JSON.read_text(encoding="utf-8")
                    ).get("tests")
                    or 0
                )
            except ValueError:
                tests = 0
    if tests <= 0:
        print("无法确定用例数：请加 --collect 或 --tests")
        return 1

    budget = _load_budget(Path(args.budget))
    coverage = _measure(Path(args.coverage), budget)

    if args.check:
        errors = _check(tests, coverage, budget)
        if errors:
            for error in errors:
                print(f"FAIL {error}")
            print(
                "\n指标快照已过期；重跑 "
                "`python scripts/snapshot_metrics.py --coverage coverage.json "
                "--collect --write`。"
            )
            return 1
        print("指标快照与当前覆盖率一致（容差内）。")
        return 0

    markdown, payload = _render(tests, coverage, budget)
    if args.write:
        METRICS_MD.write_text(markdown, encoding="utf-8")
        METRICS_JSON.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {METRICS_MD} 与 {METRICS_JSON}")
        return 0
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
