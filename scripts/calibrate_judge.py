"""文档 judge 阈值校准（手动）。

输入 JSONL，每行：
    {"question": "...", "answer": "...", "sources": [...], "label": "pass|fail"}

输出使 F1 最大的阈值，可直接写进 AGENT_DOCUMENT_JUDGE_PASS_SCORE。
默认只跑确定性 judge；加 --llm 会额外调用模型（需要 API Key）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "eval"))

from ubismart.config import get_settings  # noqa: E402
from ubismart.knowledge.judge import (  # noqa: E402
    deterministic_judgement,
    judge_document_answer,
)
from ubismart.knowledge.judge_calibration import (  # noqa: E402
    calibrate_threshold,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="", help="标注 JSONL")
    parser.add_argument(
        "--dataset",
        choices=["seed"],
        default="",
        help="使用内置种子标注集（tests/eval/judge_dataset.py）",
    )
    parser.add_argument(
        "--write-template",
        default="",
        help="把种子标注集导出为 JSONL 后退出，便于人工扩充",
    )
    parser.add_argument("--output", default="", help="把结果写入 JSON")
    parser.add_argument(
        "--llm", action="store_true", help="使用 LLM judge（需要 API Key）"
    )
    return parser


async def _score_rows(
    rows: list[dict], *, use_llm: bool
) -> list[dict]:
    settings = get_settings()
    pass_score = int(settings.document_judge_pass_score)
    if not use_llm:
        return [
            {
                "score": deterministic_judgement(
                    str(row.get("answer", "")),
                    list(row.get("sources") or []),
                    pass_score=pass_score,
                )["score"],
                "label": row.get("label"),
            }
            for row in rows
        ]
    from ubismart.llm.deepseek import build_llm

    llm = build_llm(settings, streaming=False)
    results = await asyncio.gather(
        *(
            judge_document_answer(
                llm,
                str(row.get("question", "")),
                str(row.get("answer", "")),
                list(row.get("sources") or []),
                pass_score=pass_score,
            )
            for row in rows
        )
    )
    return [
        {"score": item.get("score", 0), "label": rows[index].get("label")}
        for index, item in enumerate(results)
    ]


def _load_dataset(dataset: str) -> list[dict]:
    if dataset == "seed":
        from judge_dataset import JUDGE_CASES

        return [dict(item) for item in JUDGE_CASES]
    return []


def main() -> int:
    args = _parser().parse_args()
    if args.write_template:
        from judge_dataset import write_jsonl

        write_jsonl(args.write_template)
        print(f"已导出种子标注集：{args.write_template}")
        return 0
    rows: list[dict] = []
    if args.dataset:
        rows = _load_dataset(args.dataset)
    elif args.input:
        with Path(args.input).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    else:
        print("需要 --input 或 --dataset seed", file=sys.stderr)
        return 2
    scored = asyncio.run(_score_rows(rows, use_llm=args.llm))
    result = calibrate_threshold(scored)
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
