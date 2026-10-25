"""real 套件的结构守卫。

real 任务用完整标准库模块做种子，因此比 synthetic 任务更容易出两类错：

1. 注入锚点在新的标准库版本里不唯一/不存在 —— 任务会静默变成“没有回归”；
2. 参考解不是原文，而是被大改过的另一份代码 —— 任务不可满足或语义变了。

这里同时守住：种子必失败、参考解必通过、改动必须是**小范围**的。
"""

from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from harness import DEEP, REAL_CODE, REAL_MULTI, REAL_SPEC  # noqa: E402
from tasks_real import TASKS_REAL, TASKS_REAL_ADVANCED  # noqa: E402

ALL_TASKS = [*TASKS_REAL, *TASKS_REAL_ADVANCED]
IDS = [task.id for task in ALL_TASKS]


def _write(root: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _write_seed(task, root: Path) -> None:
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


def test_real_ids_are_unique_and_prefixed() -> None:
    assert len(ALL_TASKS) >= 11
    assert len(set(IDS)) == len(IDS)
    assert all(task_id.startswith("real/") for task_id in IDS)


def test_real_tasks_use_real_code_and_small_regressions() -> None:
    for task in ALL_TASKS:
        assert task.category in {REAL_CODE, REAL_SPEC, REAL_MULTI}
        assert task.tier == DEEP
        assert len(task.sources) == 1
        relpath, seed = next(iter(task.sources.items()))
        reference = task.reference[relpath]
        assert len(seed) > 10_000, f"{task.id} 的源文件不像真实代码"
        diff = list(
            difflib.unified_diff(
                reference.splitlines(),
                seed.splitlines(),
                lineterm="",
            )
        )
        changed = [
            line
            for line in diff
            if line.startswith(("+", "-"))
            and not line.startswith(("+++", "---"))
        ]
        assert 1 <= len(changed) <= 8, (
            f"{task.id} 的注入回归改动过大：{len(changed)} 行"
        )


@pytest.mark.parametrize("task", ALL_TASKS, ids=IDS)
def test_real_files_are_valid_python(task) -> None:
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


@pytest.mark.parametrize("task", ALL_TASKS, ids=IDS)
def test_real_seed_is_unsolved(task, tmp_path: Path) -> None:
    _write_seed(task, tmp_path)
    proc = _judge(tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1 and "FAILED" in output, (
        f"{task.id} 的种子状态判定异常（退出码 {proc.returncode}）。"
        f"退出码 0 说明注入的回归没有生效。\n{output[-1200:]}"
    )


@pytest.mark.parametrize("task", ALL_TASKS, ids=IDS)
def test_real_reference_passes(task, tmp_path: Path) -> None:
    _write_seed(task, tmp_path)
    _write(tmp_path, task.reference)
    proc = _judge(tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"{task.id} 的标准库原文没有通过判定（退出码 {proc.returncode}）。\n"
        f"{output[-1500:]}"
    )


def test_real_tasks_have_hidden_tests() -> None:
    for task in ALL_TASKS:
        assert task.hidden_tests, f"{task.id} 没有隐藏测试"
        assert set(task.tests).isdisjoint(task.hidden_tests)
