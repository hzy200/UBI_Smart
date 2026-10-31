"""架构评测任务的静态自检（不跑 WSL/模型）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from tasks_arch import TASKS_ARCH  # noqa: E402


def test_arch_tasks_have_hidden_tests_and_references() -> None:
    ids = {task.id for task in TASKS_ARCH}

    assert ids == {
        "arch/interface_rename",
        "arch/currency_propagation",
    }
    for task in TASKS_ARCH:
        assert task.hidden_tests, task.id
        assert task.reference, task.id
        # 参考解只改源码，且覆盖对外入口。
        assert set(task.reference) <= set(task.sources)
        assert "app/api.py" in task.reference


def test_arch_tasks_are_cross_file_and_deep() -> None:
    for task in TASKS_ARCH:
        assert task.category == "cross_file"
        assert task.tier == "deep"
