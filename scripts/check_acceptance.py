"""验收台账的**覆盖方式声明**检查。

`docs/acceptance/acceptance-criteria.yaml` 是"这个需求做完了没有"的唯一来源。
引用漂移（`tests:` 指向的文件被改名）已由 `scripts/check_docs.py` 的
「验收锚点」检查守着；这里补的是另一半：**每条都要说明由什么承担验收**。

- 有非空 `tests:` —— 自动覆盖；
- 没有 tests 但有 `note:` —— 显式声明为非自动化（脚本 / 真机 / 文档）；
- `status: planned` —— 还没实现，豁免。

三者都不满足的条目就是"静默缺失"：读台账的人以为有人守着，实际没有。
这类腐烂没有报警，只能靠这条规则。

用法：
    python scripts/check_acceptance.py            # 人读
    python scripts/check_acceptance.py --check    # 只返回退出码（CI）
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CRITERIA = ROOT / "docs" / "acceptance" / "acceptance-criteria.yaml"


def check(path: Path = CRITERIA) -> tuple[list[str], dict[str, int]]:
    """返回 (错误列表, 统计)。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = data.get("criteria") or []
    stats = {
        "total": len(items),
        "automated": 0,
        "declared": 0,
        "planned": 0,
        "undeclared": 0,
    }
    errors: list[str] = []
    seen: set[str] = set()

    for item in items:
        wid = str(item.get("id") or "?")
        if wid in seen:
            errors.append(f"{wid}: id 重复")
        seen.add(wid)

        if str(item.get("status") or "") == "planned":
            stats["planned"] += 1
            continue

        tests = item.get("tests") or []
        note = str(item.get("note") or "").strip()
        if tests:
            stats["automated"] += 1
        elif note:
            stats["declared"] += 1
        else:
            stats["undeclared"] += 1
            errors.append(
                f"{wid}: 既没有 tests 也没有 note（未声明由什么承担验收）"
            )
    return errors, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验收台账的覆盖方式检查")
    parser.add_argument("--check", action="store_true", help="只返回退出码")
    parser.add_argument("--path", type=Path, default=CRITERIA)
    args = parser.parse_args(argv)

    errors, stats = check(args.path)
    if errors:
        print("验收台账有问题：")
        for item in errors:
            print(f"  - {item}")
        return 1
    print(
        f"验收条目 {stats['total']} 条：自动 {stats['automated']} / "
        f"已声明非自动 {stats['declared']} / planned {stats['planned']} / "
        f"未声明 {stats['undeclared']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
