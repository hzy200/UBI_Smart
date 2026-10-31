"""长程任务上下文/worker 成本的 A/B 实验。

两臂：

- `baseline`：当前默认（worker 继承主模型，repo_map 4000，worker 上下文 8000）。
- `optimized`：worker 用 `deepseek-flash`，repo_map 1500，worker 上下文 3000，
  单 worker 报告 2000，单文件读取上限 200KB。

只跑指定的 long 任务；默认两个最贵的任务、各 2 次。结果同时打印并可选写 JSON。
这不是 CI 用例：真实调用模型，成本较高。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "eval"))

from harness import run_task, task_root  # noqa: E402
from tasks_long import LONG_TASKS  # noqa: E402

from ubismart.config import get_settings  # noqa: E402
from ubismart.sandbox.fs import SandboxFs  # noqa: E402
from ubismart.sandbox.wsl_exec import (  # noqa: E402
    WslSandbox,
    resolve_workspace,
)

ARMS = {
    "baseline": {},
    "optimized": {
        "orchestrator_worker_model": "deepseek-flash",
        "repo_map_chars": 1_500,
        "orchestrator_context_chars": 3_000,
        "explore_max_report_chars": 2_000,
        "max_file_read_bytes": 200_000,
    },
}

DEFAULT_TASKS = ("long/discount_spec", "long/discount_pipeline")


def _row(task_id: str, arm: str, repeat: int, result) -> dict:
    return {
        "task": task_id,
        "arm": arm,
        "repeat": repeat,
        "verdict": result.verdict,
        "main_in": int(result.input_tokens or 0),
        "main_out": int(result.output_tokens or 0),
        "worker_in": int(result.worker_input_tokens or 0),
        "worker_out": int(result.worker_output_tokens or 0),
        "seconds": round(result.agent_seconds, 1),
        "budget_exceeded": result.budget_exceeded,
        "detail": result.detail[-300:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="长程任务上下文/worker 成本 A/B")
    parser.add_argument(
        "--tasks",
        default=",".join(DEFAULT_TASKS),
        help="逗号分隔的任务 id",
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--json", default="", help="结果写入的 JSON 路径")
    args = parser.parse_args(argv)

    by_id = {task.id: task for task in LONG_TASKS}
    task_ids = [item.strip() for item in args.tasks.split(",") if item.strip()]
    missing = [item for item in task_ids if item not in by_id]
    if missing:
        print(f"未知任务：{missing}")
        return 2

    settings = get_settings()
    sandbox = WslSandbox(settings)
    fs = SandboxFs(sandbox, resolve_workspace(settings, sandbox))
    rows: list[dict] = []

    for task_id in task_ids:
        task = by_id[task_id]
        for arm, overrides in ARMS.items():
            scoped = settings.model_copy(update=overrides)
            for repeat in range(1, max(1, args.repeats) + 1):
                print(
                    f"→ {task_id} [{arm}] {repeat}/{args.repeats} ...",
                    flush=True,
                )
                result = asyncio.run(
                    run_task(
                        task,
                        scoped,
                        sandbox,
                        fs,
                        task_root(scoped, sandbox, task.id),
                    )
                )
                row = _row(task_id, arm, repeat, result)
                rows.append(row)
                print(
                    f"  {row['verdict']} main_in={row['main_in']} "
                    f"worker_in={row['worker_in']} {row['seconds']}s",
                    flush=True,
                )

    print("\n任务 / 臂 / 轮次 / 结果 / main_in / worker_in / 秒")
    for row in rows:
        print(
            f"{row['task']:<30} {row['arm']:<9} {row['repeat']} "
            f"{row['verdict']:<7} {row['main_in']:>9} "
            f"{row['worker_in']:>9} {row['seconds']:>7}"
        )

    print("\n合计")
    for arm in ARMS:
        selected = [row for row in rows if row["arm"] == arm]
        print(
            f"{arm:<9} runs={len(selected)} "
            f"passed={sum(r['verdict'] == 'passed' for r in selected)} "
            f"main_in={sum(r['main_in'] for r in selected)} "
            f"worker_in={sum(r['worker_in'] for r in selected)} "
            f"seconds={round(sum(r['seconds'] for r in selected), 1)}"
        )

    if args.json:
        Path(args.json).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
