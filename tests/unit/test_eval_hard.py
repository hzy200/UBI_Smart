"""hard 套件的结构守卫。

hard 任务比 default/full 更难，但“难”不能成为“不可满足”的借口：
每个任务必须有参考解，种子必须在判定下失败，参考解必须通过。

这些用例不需要 WSL、不需要 API Key，进 CI。
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from harness import (  # noqa: E402
    CONCURRENCY,
    CROSS_FILE,
    DEBUG,
    DEEP,
    PERFORMANCE,
    SPEC,
)
from tasks_hard import TASKS_HARD  # noqa: E402

IDS = [task.id for task in TASKS_HARD]
ALLOWED_CATEGORIES = {CONCURRENCY, CROSS_FILE, DEBUG, PERFORMANCE, SPEC}


def _write(root: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _write_seed(task, root: Path) -> None:
    # 判定时隐藏测试也会写进去：spec 任务的种子靠它们才会失败。
    _write(root, {**task.sources, **task.tests, **task.hidden_tests})


def _judge(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def test_hard_ids_are_unique_and_prefixed() -> None:
    assert len(TASKS_HARD) >= 12, "hard 套件至少要有 12 个任务"
    assert len(set(IDS)) == len(IDS), "hard 任务 ID 重复"
    assert all(task_id.startswith("hard/") for task_id in IDS)


def test_hard_categories_and_tier() -> None:
    per_category = Counter(task.category for task in TASKS_HARD)
    assert set(per_category) <= ALLOWED_CATEGORIES
    assert per_category[CONCURRENCY] >= 3
    assert per_category[SPEC] >= 2
    assert per_category[PERFORMANCE] >= 2
    assert {task.tier for task in TASKS_HARD} == {DEEP}


@pytest.mark.parametrize("task", TASKS_HARD, ids=IDS)
def test_hard_task_has_reference_and_tests(task) -> None:
    assert task.reference, f"{task.id} 必须提供参考解"
    assert task.tests, f"{task.id} 必须有可见测试"
    assert task.reference.keys() <= task.sources.keys(), (
        f"{task.id} 的参考解只能覆盖 sources 里的文件"
    )


@pytest.mark.parametrize("task", TASKS_HARD, ids=IDS)
def test_hard_files_are_valid_python(task) -> None:
    for relpath, content in {
        **task.sources,
        **task.judge_files,
    }.items():
        if not relpath.endswith(".py"):
            continue
        try:
            compile(content, relpath, "exec")
        except SyntaxError as exc:
            pytest.fail(f"{task.id} 的 {relpath} 有语法错误：{exc}")


@pytest.mark.parametrize("task", TASKS_HARD, ids=IDS)
def test_hard_seed_is_unsolved(task, tmp_path: Path) -> None:
    _write_seed(task, tmp_path)
    proc = _judge(tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1 and "FAILED" in output, (
        f"{task.id} 的种子状态判定异常（退出码 {proc.returncode}）。"
        f"退出码 0 说明任务在种子状态就已通过。\n{output[-1000:]}"
    )


@pytest.mark.parametrize("task", TASKS_HARD, ids=IDS)
def test_hard_reference_passes(task, tmp_path: Path) -> None:
    _write_seed(task, tmp_path)
    _write(tmp_path, task.reference)
    proc = _judge(tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"{task.id} 的参考解没有通过判定（退出码 {proc.returncode}）。\n"
        f"{output[-1500:]}"
    )


@pytest.mark.parametrize(
    "task", [t for t in TASKS_HARD if t.category == SPEC], ids=lambda t: t.id
)
def test_hard_spec_tasks_have_hidden_tests(task) -> None:
    assert task.hidden_tests, f"{task.id} 属于 spec 类却没有隐藏测试"
    assert set(task.tests).isdisjoint(task.hidden_tests), (
        f"{task.id} 的可见测试与隐藏测试文件名冲突"
    )
