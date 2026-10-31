"""长文档效果评测（手动，需要 WSL + API Key）。

用法：
    python scripts/eval_documents.py --file docs/report.pdf --question "提炼关键结论"

输出过程指标：覆盖页数/比例、未读区间、citation 是否带锚点/原文、token 用量。
答案正确性仍需人工或 LLM judge；这里不把“看起来完整”当成“答对了”。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "eval"))

from doc_metrics import summarize_document_run  # noqa: E402

from ubismart.config import get_settings  # noqa: E402
from ubismart.knowledge.judge import judge_document_answer  # noqa: E402
from ubismart.llm.deepseek import build_llm  # noqa: E402
from ubismart.orchestrator.document_workers import (  # noqa: E402
    make_document_runner,
)
from ubismart.sandbox.backend import build_sandbox  # noqa: E402
from ubismart.tools.registry import build_tools  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", action="append", default=[], help="工作区内的文档路径，可重复"
    )
    parser.add_argument("--question", required=True, help="要回答/提炼的问题")
    parser.add_argument("--model", default="", help="覆盖模型名")
    parser.add_argument(
        "--judge", action="store_true", help="启用答案 judge（确定性）"
    )
    parser.add_argument(
        "--judge-llm", action="store_true", help="judge 额外调用模型"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


async def _run(args: argparse.Namespace) -> dict:
    settings = get_settings().model_copy(
        update={
            "deep_workflows_enabled": True,
            "document_full_coverage": True,
            "document_map_reduce": True,
        }
    )
    sandbox = build_sandbox(settings)
    tools = build_tools(settings, sandbox=sandbox, allow_write=False)
    llm = build_llm(
        settings,
        streaming=False,
        model_override=args.model,
    )
    runner = make_document_runner(
        llm=llm, tools=tools, settings=settings
    )
    result = await runner(
        {
            "id": "eval-doc",
            "role": "document",
            "goal": args.question,
            "inputs": list(args.file),
            "knowledge_run_id": f"eval-{int(time.time())}",
        },
        parent_request=args.question,
    )
    metrics = summarize_document_run(result.model_dump())
    metrics["answer"] = str(result.summary)
    metrics["error"] = str(result.error)
    metrics["duration_ms"] = int(result.duration_ms)
    metrics["findings_detail"] = [str(item) for item in result.findings]
    metrics["sources_detail"] = [
        dict(item) for item in result.sources if isinstance(item, dict)
    ]
    if args.judge:
        answer = "\n".join(
            [str(result.summary), *[str(item) for item in result.findings]]
        )
        metrics["judgement"] = await judge_document_answer(
            llm if args.judge_llm else None,
            args.question,
            answer,
            list(result.sources),
            pass_score=int(settings.document_judge_pass_score),
        )
    return metrics


def main() -> int:
    args = _parser().parse_args()
    if not args.file:
        print("至少需要一个 --file", file=sys.stderr)
        return 2
    metrics = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        coverage = metrics["coverage"]
        citations = metrics["citations"]
        print(
            f"coverage={coverage['coverage_ratio']:.0%} "
            f"({coverage['read_pages']}/{coverage['total_pages']} pages), "
            f"status={metrics['status']}"
        )
        if coverage["unread_ranges"]:
            print("unread:", "、".join(coverage["unread_ranges"]))
        print(
            f"citations={citations['citations']} "
            f"anchor={citations['anchor_ratio']:.0%} "
            f"quote={citations['quote_ratio']:.0%} "
            f"quote_verified={citations['quote_verified_ratio']:.0%}"
        )
        print(
            f"tokens: in={metrics['input_tokens']} "
            f"out={metrics['output_tokens']}"
        )
        judgement = metrics.get("judgement")
        if judgement:
            print(
                f"judge={judgement['mode']} score={judgement['score']} "
                f"supported={judgement['supported']}"
            )
            for issue in judgement.get("citation_issues") or []:
                print("  citation:", issue)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
