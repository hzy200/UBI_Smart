"""导出 150 条待人工审核的 judge 标注包。

人工逐条填写 `label`（pass/fail）、`reviewer`、`reviewed_at`，并把
`label_source` 改为 `human`；或用 `judge_human_pack.promote_case` 转换。
未审核的 pending 数据不能进入校准。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "eval"))

from judge_human_pack import export_pending  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="输出 JSONL")
    parser.add_argument("--target", type=int, default=150)
    args = parser.parse_args()
    export_pending(args.out, args.target)
    print(f"已导出 {args.target} 条待审核样本：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
