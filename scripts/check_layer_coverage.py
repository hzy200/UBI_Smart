"""按层校验覆盖率：安全承重墙不许被全局数字掩盖。

为什么需要它：`pyproject.toml` 的 `fail_under=76` 是**全局**门槛。CI 跑在
Linux 容器里、没有 WSL2，于是执行真实命令的那一层一行都没跑，它的低覆盖
被其它模块的高覆盖稀释掉 —— "CI 全绿"读起来像"沙箱层也没问题"，事实相反。

本脚本在**真的执行了沙箱层**的 runner 上（带 WSL2 的 self-hosted）跑，
对这些模块单独设下限。门槛只在能跑的机器上才有意义，所以 Linux CI 不调它。

用法：
    pytest -m "not llm" --cov --cov-report=json:coverage.json
    python scripts/check_layer_coverage.py --json coverage.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 只列安全承重墙：路径守卫、写前快照、命令分级、文件写工具。
# 门槛按"这一层必须被真实执行过"来定，不追求 100%。
LAYER_FLOORS: dict[str, float] = {
    "sandbox/fs.py": 70.0,
    "sandbox/snapshots.py": 70.0,
    "sandbox/pathguard.py": 85.0,
    "sandbox/policy.py": 85.0,
    "tools/files.py": 60.0,
}


def _pct(entry: dict) -> float:
    summary = entry.get("summary") or {}
    statements = int(summary.get("num_statements") or 0)
    if statements == 0:
        return 100.0
    covered = statements - int(summary.get("missing_lines") or 0)
    return covered * 100.0 / statements


def _relative(filename: str) -> str:
    """`src/coding_agent/sandbox/fs.py` → `sandbox/fs.py`。"""
    marker = "coding_agent/"
    text = str(filename).replace("\\", "/")
    index = text.rfind(marker)
    return text[index + len(marker) :] if index >= 0 else text


def check(report: dict) -> list[str]:
    files = report.get("files") or {}
    measured: dict[str, float] = {}
    for filename, entry in files.items():
        relative = _relative(str(filename))
        if relative in LAYER_FLOORS:
            measured[relative] = _pct(entry)

    errors: list[str] = []
    for module, floor in LAYER_FLOORS.items():
        if module not in measured:
            errors.append(
                f"{module}：覆盖率报告里没有它（被测集合变了？）"
            )
            continue
        actual = measured[module]
        if actual < floor:
            errors.append(
                f"{module}：{actual:.1f}% < 门槛 {floor:.0f}%"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        default="coverage.json",
        help="`--cov-report=json:<path>` 产出的报告",
    )
    args = parser.parse_args(argv)

    path = Path(args.json)
    if not path.is_file():
        print(f"找不到覆盖率报告：{path}")
        return 1
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"覆盖率报告无法解析：{exc}")
        return 1

    errors = check(report)
    for module in LAYER_FLOORS:
        measured = {
            _relative(name): _pct(entry)
            for name, entry in (report.get("files") or {}).items()
            if _relative(str(name)) == module
        }
        for name, value in measured.items():
            print(f"{name}: {value:.1f}%（门槛 {LAYER_FLOORS[module]:.0f}%）")

    if errors:
        print("\n沙箱层覆盖率未达标：")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("\n沙箱层覆盖率达标。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
