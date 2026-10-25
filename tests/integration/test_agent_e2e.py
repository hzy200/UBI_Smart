"""智能体级端到端验收：真实 WSL + 真实模型。

`pytest -m "not llm"` **不会**执行本文件 —— 这里每个用例都会真实调用模型并计费。
显式运行：

```bash
AGENT_WSL_DISTRO=CodingAgent pytest -m agent_e2e \
    tests/integration/test_agent_e2e.py -q
```

覆盖的是**智能体本身**，而不是单个函数：

- `direct`：读代码 → 改源码 → 自动验证 → 判定；
- `orchestrated_implement`：工作流选择 → 任务图 → 并发 implement worker →
  git worktree 隔离 → 补丁合并 → 判定。

审批暂停/恢复、快照回滚、checkpoint 续跑不在这里重复：它们由
`scripts/phase_g_e2e.py` 在 A3+A4 全链路上真实验收（需要专用发行版与密钥）。
本文件补的是「pytest 里有没有一个可重复调用的智能体验收入口」。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from harness import run_task, task_root  # noqa: E402
from tasks import TASKS  # noqa: E402
from tasks_long import LONG_TASKS  # noqa: E402

# 同时带 `llm`：默认全量 `-m "not llm"` 会跳过，只有显式 `-m agent_e2e` 才跑。
#
# 刻意**不标 `wsl`**：`pytest -m wsl` 是“只跑沙箱层”的常用入口，若这里也带
# wsl，那条命令会顺带调用真实模型。真实沙箱的放行由 conftest 单独认
# `agent_e2e` 标记。
pytestmark = [
    pytest.mark.llm,
    pytest.mark.agent_e2e,
]


def _settings(**overrides) -> Settings:
    """真实运行的设置。

    不用 `_env_file=None`：密钥与模型配置就在 `.env` 里，测试要复用同一套
    环境。conftest 已隔离控制面数据库，这里不受影响。
    """
    values: dict[str, object] = {
        "orchestrator_enabled": True,
        "orchestrator_min_files": 0,
        "orchestrator_write_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _require_runtime(settings: Settings) -> tuple[WslSandbox, SandboxFs]:
    if not settings.deepseek_api_key:
        pytest.skip("未配置 DEEPSEEK_API_KEY")
    sandbox = WslSandbox(settings)
    if not WslSandbox.available(settings.wsl_distro):
        pytest.skip(f"WSL 发行版 {settings.wsl_distro} 不可用")
    return sandbox, SandboxFs(sandbox, resolve_workspace(settings, sandbox))


def _task(tasks, task_id: str):
    for task in tasks:
        if task.id == task_id:
            return task
    raise AssertionError(f"评测任务不存在：{task_id}")


def test_direct_agent_completes_task() -> None:
    """单 agent 路径：工具调用、写源码、验证、判定闭环。"""
    settings = _settings()
    sandbox, fs = _require_runtime(settings)
    task = _task(TASKS, "single_file/off_by_one")

    result = asyncio.run(
        run_task(
            task,
            settings,
            sandbox,
            fs,
            task_root(settings, sandbox, task.id),
        )
    )

    assert result.verdict == "passed", result.detail
    assert result.tool_calls >= 1, "智能体一次工具都没调用"
    assert result.writes >= 1, "智能体没有写工作区（只读不算完成）"
    assert result.verifications, "验证脊柱没有被触发"
    assert result.input_tokens is not None, "provider 未回报 token 用量"


def test_orchestrated_implement_parallel_workers() -> None:
    """编排写路径：任务图、并发 implement、worktree 与合并。"""
    settings = _settings(
        orchestrator_write_enabled=True,
        orchestrator_worker_rounds=12,
    )
    sandbox, fs = _require_runtime(settings)
    task = _task(LONG_TASKS, "long/parallel_modules")

    result = asyncio.run(
        run_task(
            task,
            settings,
            sandbox,
            fs,
            task_root(settings, sandbox, task.id),
        )
    )

    assert result.verdict == "passed", result.detail
    # 请求里有「两件事一起做 / 分别完成」的多交付物信号，规则层会确定性地
    # 选择写编排；这里断言规则与执行都对得上。
    assert result.workflow == "orchestrated_implement", result.workflow
    assert result.tasks_planned >= 2, f"任务图只有 {result.tasks_planned} 个任务"
    assert result.workers >= 2, f"只启动了 {result.workers} 个 worker"
    assert result.worker_roles.get("implement", 0) >= 1, result.worker_roles
    assert result.worker_tool_calls >= 1, "implement worker 没有调用工具"
    assert result.worker_merge_attempts >= 1, "没有发生补丁合并"
    assert result.worker_merged >= 1, "补丁一个都没合并成功"
    assert result.worker_merged <= result.worker_merge_attempts
