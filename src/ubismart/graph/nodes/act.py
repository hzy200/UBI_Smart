"""act 节点：调用模型，产出「工具意图」或「当前步骤的小结」。

关键不变量
----------
**act 只在还有轮次预算时才产出 tool_calls。**

这条不变量让路由可以无条件地把带 tool_calls 的消息送去 tools 执行，
从而绝不会留下「有 tool_calls 却没人应答」的消息 —— 那会让下一轮
OpenAI 兼容接口直接报错。超预算时 act 自己产出一条无 tool_calls 的收尾消息。

配置透传
--------
config 原样传给模型，LangGraph 因此能挂上 token 回调，
CLI 侧用 stream_mode="messages" 实现逐字流式输出。

**这里是 async 节点，走 `llm.ainvoke`。**
同步的 `invoke` 会被 LangGraph 丢进线程池执行：既占满一个线程，又让 Ctrl-C
在模型生成期间完全失效（取消信号传不进线程池里的阻塞 HTTP 调用）。
改用 `ainvoke` 之后取消与超时都能顺着事件循环传下去。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from ubismart.graph.plan import EXPLORE, IMPLEMENT, current_step_kind
from ubismart.graph.state import AgentState
from ubismart.llm.context import (
    ContextBudget,
    is_context_length_error,
    summarize_messages,
    tighter_budget,
    trim_messages,
)
from ubismart.llm.prompts import (
    DELEGATION_NOTE,
    DELEGATION_NOTE_DISABLED,
    DELEGATION_NOTE_NOT_EXPLORE,
    compose_system_prompt,
    format_review_feedback,
    format_verification_feedback,
)
from ubismart.session.recovery import retry_async

BUDGET_EXHAUSTED_TEMPLATE = "（本步骤已达到工具调用上限 {limit} 轮，停止继续尝试。）"


@dataclass
class _ActDeps:
    llm: BaseChatModel
    max_tool_rounds: int
    allow_write: bool
    max_repair_rounds: int
    trim_budget: ContextBudget
    delegate_enabled: bool
    forced_llm: BaseChatModel | None
    retry_attempts: int
    retry_base_delay: float
    retry_max_delay: float
    retry_max_total_wait: float


def _feedback(state: AgentState, max_repair_rounds: int) -> str:
    # 上一步被质量关拦下时，把结构化问题合成进系统提示 ——
    # 不写回消息历史，否则每轮重试都在往上下文里塞一条伪造口吻的消息。
    #
    # **审查优先**：它总是发生在验证之后，所以它的结论更新。两者也不会同时
    # 成立（审查只在验证通过后才跑），只是优先级把这件事写明确。
    verification = state.get("verification") or {}
    review = state.get("review") or {}
    attempt = state.get("retry", 0)
    if review.get("status") == "blocked":
        return format_review_feedback(
            review, attempt=attempt, limit=max_repair_rounds or attempt
        )
    if verification.get("status") == "failed" and attempt > 0:
        return format_verification_feedback(
            verification,
            attempt=attempt,
            limit=max_repair_rounds or attempt,
            same_failure_count=int(state.get("same_failure_count", 0)),
        )
    return ""


def _delegation_note(state: AgentState, delegate_enabled: bool) -> str:
    if not delegate_enabled:
        return ""
    if not state.get("delegation_allowed", True):
        return DELEGATION_NOTE_DISABLED
    if current_step_kind(state) != EXPLORE:
        return DELEGATION_NOTE_NOT_EXPLORE
    budget = max(1, int(state.get("delegation_budget", 1)))
    return DELEGATION_NOTE.format(max_calls=budget)


def _system_prompt(
    state: AgentState,
    deps: _ActDeps,
    feedback: str,
    delegation_note: str,
) -> str:
    return compose_system_prompt(
        plan=state.get("plan"),
        plan_kinds=state.get("plan_kinds"),
        step_idx=state.get("step_idx", 0),
        step_kind=current_step_kind(state),
        allow_write=deps.allow_write,
        memories=state.get("memories"),
        project_instructions=str(state.get("project_instructions") or ""),
        stale_paths=state.get("stale_paths"),
        feedback=feedback,
        # 上一步没产生任何改动、被 nudge 打回来重做 —— 把它明说给模型听，
        # 否则它只会把同样的事再做一遍
        empty_step=bool(state.get("empty_step_nudged"))
        and not state.get("dirty"),
        # 只有 delegate 工具真的绑定时才提；否则会教模型调用一个不存在的工具
        delegation_note=delegation_note,
        worker_context=str(state.get("worker_context") or ""),
        recovery_notice=str(state.get("recovery_notice") or ""),
        context_summary=summarize_messages(state["messages"]),
    )


def _should_force_tool(state: AgentState, deps: _ActDeps) -> bool:
    # 实测 deepseek-chat 在 implement 步偶尔会连续返回「无 tool_calls」的
    # 空动作（读完文件但不动手）。被 nudge 之后的第一次调用强制 required，
    # 至少让它先读目标文件；后续调用恢复自动选择，留出编辑机会。
    return bool(
        deps.forced_llm is not None
        and current_step_kind(state) == IMPLEMENT
        and bool(state.get("empty_step_nudged"))
        and not state.get("dirty")
        and int(state.get("tool_rounds", 0)) == 0
    )


async def _invoke_model(
    model: BaseChatModel,
    messages: list[Any],
    config: RunnableConfig,
    deps: _ActDeps,
    retry_events: list[dict[str, Any]],
) -> Any:
    return await retry_async(
        lambda: model.ainvoke(messages, config),
        attempts=deps.retry_attempts,
        base_delay=deps.retry_base_delay,
        max_delay=deps.retry_max_delay,
        max_total_wait=deps.retry_max_total_wait,
        on_retry=retry_events.append,
        label="act",
    )


async def _retry_tighter(
    state: AgentState,
    config: RunnableConfig,
    deps: _ActDeps,
    feedback: str,
    delegation_note: str,
    retry_events: list[dict[str, Any]],
) -> tuple[Any, Any]:
    tighter = tighter_budget(deps.trim_budget)
    tighter_history, tighter_report = trim_messages(
        state["messages"], tighter
    )
    tighter_messages = [
        SystemMessage(
            content=_system_prompt(state, deps, feedback, delegation_note)
        ),
        *tighter_history,
    ]
    response = await _invoke_model(
        deps.llm, tighter_messages, config, deps, retry_events
    )
    return response, tighter_report


async def _recover_invoke(
    exc: Exception,
    state: AgentState,
    config: RunnableConfig,
    deps: _ActDeps,
    feedback: str,
    delegation_note: str,
    messages: list[Any],
    retry_events: list[dict[str, Any]],
    force_tool: bool,
    trim_report: Any,
) -> tuple[Any, Any]:
    if force_tool:
        # 强制 tool_choice 在个别 provider/模式下可能被拒；退回普通调用，
        # 不要让一次强制失败把整个运行打断。
        try:
            response = await _invoke_model(
                deps.llm, messages, config, deps, retry_events
            )
            return response, trim_report
        except Exception as fallback_exc:  # noqa: BLE001
            if not is_context_length_error(fallback_exc):
                raise
            return await _retry_tighter(
                state, config, deps, feedback, delegation_note, retry_events
            )
    if is_context_length_error(exc):
        return await _retry_tighter(
            state, config, deps, feedback, delegation_note, retry_events
        )
    raise


async def _act_impl(
    state: AgentState, config: RunnableConfig, deps: _ActDeps
) -> dict[str, Any]:
    rounds = state.get("tool_rounds", 0)
    if rounds >= deps.max_tool_rounds:
        return {
            "messages": [
                AIMessage(
                    content=BUDGET_EXHAUSTED_TEMPLATE.format(
                        limit=deps.max_tool_rounds
                    )
                )
            ],
            "budget_exhausted": True,
        }

    retry_events: list[dict[str, Any]] = []
    feedback = _feedback(state, deps.max_repair_rounds)
    delegation_note = _delegation_note(state, deps.delegate_enabled)
    system = _system_prompt(state, deps, feedback, delegation_note)
    history, trim_report = trim_messages(state["messages"], deps.trim_budget)
    messages = [SystemMessage(content=system), *history]

    force_tool = _should_force_tool(state, deps)
    model = deps.forced_llm if force_tool else deps.llm
    try:
        response = await _invoke_model(
            model, messages, config, deps, retry_events
        )
    except Exception as exc:  # noqa: BLE001
        response, trim_report = await _recover_invoke(
            exc,
            state,
            config,
            deps,
            feedback,
            delegation_note,
            messages,
            retry_events,
            force_tool,
            trim_report,
        )
    return {
        "messages": [response],
        "tool_rounds": rounds + 1,
        "transient_retries": len(retry_events),
        "context_trim": {
            **asdict(trim_report),
            "node": "act",
        },
    }


def make_act_node(
    llm: BaseChatModel,
    *,
    max_tool_rounds: int,
    allow_write: bool = False,
    max_repair_rounds: int = 0,
    budget: ContextBudget | None = None,
    delegate_enabled: bool = False,
    forced_llm: BaseChatModel | None = None,
    retry_attempts: int = 3,
    retry_base_delay: float = 1.0,
    retry_max_delay: float = 30.0,
    retry_max_total_wait: float = 60.0,
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    # 裁剪只影响发给模型的内容，不改动写回 state 的历史
    deps = _ActDeps(
        llm=llm,
        max_tool_rounds=max_tool_rounds,
        allow_write=allow_write,
        max_repair_rounds=max_repair_rounds,
        trim_budget=budget or ContextBudget(),
        delegate_enabled=delegate_enabled,
        forced_llm=forced_llm,
        retry_attempts=retry_attempts,
        retry_base_delay=retry_base_delay,
        retry_max_delay=retry_max_delay,
        retry_max_total_wait=retry_max_total_wait,
    )

    async def act(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        return await _act_impl(state, config, deps)

    return act
