"""构建 / 校验 / 切分 judge 标注集。

默认生成**合成标注**（label_source=synthetic），用于跑通大规模校准流程；
真实人工标注请按同一 schema 追加，并把 label_source 设为 human：

    python scripts/build_judge_dataset.py --target 1000 --out dist/judge/all.jsonl \
        --split-dir dist/judge

    # 校验只允许人工标注
    python scripts/build_judge_dataset.py --input dist/judge/all.jsonl \
        --out dist/judge/human.jsonl --require-human
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "eval"))

from judge_dataset_builder import (  # noqa: E402
    balance_stats,
    build_cases,
    dedupe_cases,
    export_jsonl,
    import_jsonl,
    split_cases,
    validate_cases,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=1_000)
    parser.add_argument("--input", default="", help="已标注 JSONL")
    parser.add_argument("--out", required=True, help="输出 JSONL")
    parser.add_argument("--split-dir", default="", help="输出 train/calibration/test")
    parser.add_argument(
        "--require-human", action="store_true", help="只接受 label_source=human"
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    cases = (
        import_jsonl(args.input) if args.input else build_cases(args.target)
    )
    cases = dedupe_cases(cases)
    errors = validate_cases(cases, require_human=args.require_human)
    if errors:
        print("\n".join(f"- {item}" for item in errors[:50]), file=sys.stderr)
        return 2
    export_jsonl(cases, args.out)
    split = split_cases(cases)
    if args.split_dir:
        for name, items in split.items():
            export_jsonl(items, Path(args.split_dir) / f"{name}.jsonl")
    print(
        json.dumps(
            {
                "out": args.out,
                "split_dir": args.split_dir,
                "balance": balance_stats(cases),
                "split": {name: len(items) for name, items in split.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
