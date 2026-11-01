"""verify 节点：一步做完后跑一遍项目的测试/构建，把结果结构化。

两个刻意的取舍：

1. **只在改过东西时才验证**（`dirty`）。只读探索的步骤跑测试纯属浪费 ——
   一次 pytest 可能几十秒。
2. **自动验证不走审批**，因此它只能跑**命令文本完全由宿主确定**的命令。
   判据是「谁决定执行什么」：`cargo test` / `go test` / `pytest` 跑什么由宿主
   写死；而 `make test` 跑什么写在 Makefile 里、`npm test` 写在 package.json 里，
   这两个文件模型写得到（`--write` 下是自动放行的 L1）—— 那样就出现了一条
   「写个恶意 Makefile → 下一次脏写自动执行」的无审批执行路径，绕过命令分级。
   所以这里传 `allow_manifest=False`；模型主动调 `run_tests` 才走审批，
   那条路径才允许 manifest 派生的命令。

验证失败**不会中断任务**：结果写进状态、事件与审计，由后续路由决定怎么办
（W10 会在这里接 repair 循环；当前是如实报告给 respond）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from ubismart.graph.plan import (
    EXPLORE,
    IMPLEMENT,
    current_step_kind,
)
from ubismart.graph.plan import (
    VERIFY as VERIFY_KIND,
)
from ubismart.graph.state import AgentState
from ubismart.sandbox.wsl_exec import WslSandbox, resolve_workspace
from ubismart.tools.testrun import VerifyResult, run_verification


def _skip_update(
    state: AgentState, sandbox: WslSandbox
) -> dict[str, Any] | None:
    pending = bool(state.get("pending_verification"))
    if not sandbox.settings.verify_enabled:
        # 用户显式关闭验证时，不能让这个标志永远挂着（否则每次都会再判断一遍）。
        return (
            {"verification": {}, "pending_verification": False}
            if pending
            else {"verification": {}}
        )

    kind = current_step_kind(state)
    # 探索步是只读的。即便某条 shell 命令被误判成写（dirty=True），
    # 也不该让它跑全量测试 —— 后续实现/验证步会兜住真正的问题。
    # 例外：编排刚合并过补丁，此时验证的是合并结果，不是这一步的探索。
    if kind == EXPLORE and not pending:
        return {"verification": {}}

    # kind=verify 的步骤就是用来跑全量验证的：即使这一步没有新改动
    # （dirty=False，比如前面的实现步已经写完），也要跑。
    if kind != VERIFY_KIND and not state.get("dirty") and not pending:
        return {"verification": {}}

    # 多步任务：早期步骤只完成了一部分，跑全量套件必然因为后面还没做的
    # 步骤而失败，会把 agent 钉死在这一步。只在最终步（或 kind=verify）
    # 验证；单步任务（也是最后一步）照常验证。
    if sandbox.settings.verify_only_final:
        plan = state.get("plan") or []
        step_idx = int(state.get("step_idx", 0))
        is_last = step_idx >= len(plan) - 1
        if not is_last and kind != VERIFY_KIND:
            if kind == IMPLEMENT or pending:
                return {"verification": {}}
    return None


def _same_failure_count(
    state: AgentState, result: Any, previous: dict[str, Any]
) -> int:
    previous_signature = str(
        state.get("last_failure_signature")
        or previous.get("failure_signature")
        or ""
    )
    result_status = str(getattr(result, "status", ""))
    result_signature = str(getattr(result, "failure_signature", "") or "")
    if (
        result_status == "failed"
        and result_signature
        and result_signature == previous_signature
    ):
        return int(state.get("same_failure_count", 0)) + 1
    return 0


def _verify_update(
    state: AgentState, result: VerifyResult, pending: bool
) -> dict[str, Any]:
    previous = state.get("verification") or {}
    result_status = str(getattr(result, "status", ""))
    result_signature = str(getattr(result, "failure_signature", "") or "")
    same_failure_count = _same_failure_count(state, result, previous)
    update: dict[str, Any] = {
        "verification": result.model_dump(),
        # 运行级快照：replan 会清 verification，但收尾要靠它判断真实结论
        "last_verification": result.model_dump(),
        "same_failure_count": same_failure_count,
        "last_failure_signature": (
            result_signature if result_status == "failed" else ""
        ),
        # 修复轮本身已经结束；是否还能再进一轮由路由按预算决定。
        "orchestrator_fix_mode": False,
        # 只有"编排合并结果的验证失败"才允许把失败交回 orchestrator。
        "orchestrator_fix_eligible": bool(
            pending and result_status == "failed"
        ),
    }
    if pending:
        # 已经为合并结果跑过一次验证（无论通过与否），后续交给 repair/replan。
        update["pending_verification"] = False
    return update


def _verify_impl(
    state: AgentState, config: RunnableConfig, sandbox: WslSandbox, root: str
) -> dict[str, Any]:
    skip = _skip_update(state, sandbox)
    if skip is not None:
        return skip

    pending = bool(state.get("pending_verification"))
    previous = state.get("verification") or {}
    # repair 的快速通道：只重跑上次失败的测试；子集通过后 runner 会自动
    # 补跑完整命令，所以不会漏掉回归。
    only: tuple[str, ...] = ()
    if int(state.get("retry", 0)) > 0:
        only = tuple(str(t) for t in (previous.get("failed_tests") or ()))

    try:
        result = run_verification(
            sandbox, root, allow_manifest=False, only=only
        )
    except Exception as exc:  # noqa: BLE001 - 环境异常不能拖垮整轮
        # 这是图上的一道质量关，不是运行本身。WSL 掉线/命令启动失败
        # 应当记成「没验证成功」，而不是让整次 RunFailed；更不能进入
        # repair —— 模型修不了一个坏掉的执行环境。
        result = VerifyResult(
            status="error",
            kind="none",
            summary=f"{type(exc).__name__}: {exc}",
        )

    return _verify_update(state, result, pending)


def make_verify_node(
    sandbox: WslSandbox,
    root: str | None = None,
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    """sandbox 是执行侧的配置源（超时、verify_enabled、verify_command 都在它的 settings 里）。

    `root` 由调用方显式给：verify 与工具共用同一个沙箱实例，而决定工作区的是
    **本次运行的 settings**（可能带 `--workspace` 覆盖），不是沙箱自己那份。
    不传则由沙箱的 settings 推 —— 那会退回 $HOME，与工具的工作区不是同一个。
    """
    root = root or resolve_workspace(sandbox.settings, sandbox)

    def verify(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        return _verify_impl(state, config, sandbox, root)

    return verify
