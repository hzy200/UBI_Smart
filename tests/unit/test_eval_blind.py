"""盲测套件的结构守卫。

盲测任务**没有可见测试**：模型只能从症状出发定位回归。这里守住三件事：

1. 确实没有可见测试（否则就不叫盲测）；
2. 隐藏测试存在，且种子在判定下失败；
3. 参考解（标准库原文）能通过隐藏测试。
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

from harness import DEEP, REAL_BLIND  # noqa: E402
from tasks_real import TASKS_BLIND  # noqa: E402

IDS = [task.id for task in TASKS_BLIND]


def _write(root: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _write_seed(task, root: Path) -> None:
    _write(root, {**task.sources, **task.hidden_tests})


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


def test_blind_ids_and_shape() -> None:
    assert len(TASKS_BLIND) >= 5
    assert len(set(IDS)) == len(IDS)
    assert all(task_id.startswith("blind/") for task_id in IDS)
    for task in TASKS_BLIND:
        assert task.category == REAL_BLIND
        assert task.tier == DEEP
        assert task.tests == {}, f"{task.id} 不应有可见测试"
        assert task.hidden_tests, f"{task.id} 没有隐藏测试"
        assert task.reference, f"{task.id} 没有参考解"


def test_blind_sources_are_real_and_changes_are_small() -> None:
    for task in TASKS_BLIND:
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
        assert 1 <= len(changed) <= 6, (
            f"{task.id} 的注入回归改动过大：{len(changed)} 行"
        )


@pytest.mark.parametrize("task", TASKS_BLIND, ids=IDS)
def test_blind_files_are_valid_python(task) -> None:
    for relpath, content in {
        **task.sources,
        **task.hidden_tests,
    }.items():
        if not relpath.endswith(".py"):
            continue
        try:
            compile(content, relpath, "exec")
        except SyntaxError as exc:
            pytest.fail(f"{task.id} 的 {relpath} 有语法错误：{exc}")


@pytest.mark.parametrize("task", TASKS_BLIND, ids=IDS)
def test_blind_seed_is_unsolved(task, tmp_path: Path) -> None:
    _write_seed(task, tmp_path)
    proc = _judge(tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1 and "FAILED" in output, (
        f"{task.id} 的种子状态判定异常（退出码 {proc.returncode}）。\n"
        f"{output[-1200:]}"
    )


@pytest.mark.parametrize("task", TASKS_BLIND, ids=IDS)
def test_blind_reference_passes(task, tmp_path: Path) -> None:
    _write_seed(task, tmp_path)
    _write(tmp_path, task.reference)
    proc = _judge(tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"{task.id} 的参考解没有通过隐藏测试（退出码 {proc.returncode}）。\n"
        f"{output[-1500:]}"
    )
