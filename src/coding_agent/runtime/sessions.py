"""会话生命周期：历史回放、checkpoint、分叉、回退、压缩与收尾。

从 `core.py` 拆出，只做职责归类；方法体未改动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from coding_agent.audit import AuditRecord
from coding_agent.audit import models as audit_models
from coding_agent.audit.logger import now_iso, truncate
from coding_agent.llm.context import summarize_messages
from coding_agent.memory.sessions import SessionIndex
from coding_agent.orchestrator.merge_journal import MergeJournal
from coding_agent.protocol.messages import text_of


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    """还原历史会话时用的一轮对话。"""

    role: str  # user | assistant
    text: str


class SessionsMixin:
    """`AgentRuntime` 的会话生命周期。"""

    @property
    def sessions(self) -> SessionIndex:
        """历史会话列表（从审计日志归纳）。"""
        return SessionIndex(self.settings.resolved_audit_dir)

    async def history(
        self,
        thread_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[HistoryMessage]:
        """从 checkpoint 还原某个会话的对话（供 `/switch` 展示）。

        只取人和助手的自然语言往来；工具调用与工具结果不展示 ——
        它们是过程噪声，重新渲染只会淹没真正的对话。

        `limit`/`offset` 对**过滤后的对话**分页（按时间正序），长会话前端
        不必一次拉回全部历史；`limit=None` 保持旧行为。

        刻意**直读 checkpoint 而不建图**：回放历史不该要求 API Key 或可用的沙箱
        （`_ensure_graph` 会拉起 `build_llm` 与 WSL 探测）。
        耦合点：直接取 `channel_values["messages"]`，与 `AgentState.messages` 对应。
        """
        saver = await self._checkpointer()
        fetch = getattr(saver, "aget_tuple", None)
        if fetch is None:
            return []
        tuple_ = await fetch({"configurable": {"thread_id": thread_id}})
        if tuple_ is None:
            return []
        checkpoint = getattr(tuple_, "checkpoint", None) or {}
        messages = (checkpoint.get("channel_values") or {}).get("messages") or []

        history: list[HistoryMessage] = []
        for message in messages:
            if isinstance(message, HumanMessage):
                text = text_of(message)
                if text.strip():
                    history.append(HistoryMessage("user", text))
            elif isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
                text = text_of(message)
                if text.strip():
                    history.append(HistoryMessage("assistant", text))
        offset = max(0, int(offset))
        if offset:
            history = history[offset:]
        if limit is not None:
            history = history[: max(0, int(limit))]
        return history

    async def final_state(self, thread_id: str) -> dict[str, Any]:
        """读取该 thread 最近一次 checkpoint 的状态。

        只读、不建图；评测用它取上下文画像（repo_map / worker 摘要 / 裁剪
        报告）。运行失败或 checkpoint 不可用时返回空 dict。
        """
        return await self._prior_state(thread_id)

    async def _prior_state(self, thread_id: str) -> dict[str, Any]:
        """读取该 thread 上一次 checkpoint 的状态值。

        与 `history()` 一样直读 checkpoint，不建图。读取失败只是少了一段
        上下文与一次悬空调用修复的机会，不能阻断本次运行。
        """
        try:
            saver = await self._checkpointer()
            fetch = getattr(saver, "aget_tuple", None)
            if fetch is None:
                return {}
            tuple_ = await fetch({"configurable": {"thread_id": thread_id}})
            if tuple_ is None:
                return {}
            checkpoint = getattr(tuple_, "checkpoint", None) or {}
            return dict(checkpoint.get("channel_values") or {})
        except Exception:  # noqa: BLE001 - 读取失败不阻断运行
            return {}

    def _recovery_notice(self, prior: dict[str, Any]) -> str:
        """上一轮是否中断，以及工作区可能残留什么。

        不自动回滚、也不自动重跑：只把事实交给模型和用户。自动回滚会在用户
        已经手工改过文件时造成二次破坏；自动重跑则可能重复非幂等副作用。
        """
        if str(prior.get("run_status") or "") != "running":
            return ""
        # 只在"上一轮确实没收尾"时去读合并登记：读一次要起一次沙箱进程，
        # 不能挂在每次 run 的启动路径上。
        journal_notice = ""
        try:
            journal_notice = MergeJournal(self._sandbox, self.workspace).notice()
        except Exception:  # noqa: BLE001 - 登记读不出来不该挡住运行
            journal_notice = ""
        verification = prior.get("last_verification") or {}
        status = str(verification.get("status") or "未验证")
        paths = [
            str(item) for item in (prior.get("stale_paths") or []) if str(item)
        ]
        notice = (
            "上一次运行没有正常收尾（可能因网络、沙箱或进程中断）。"
            f"工作区可能有未完成的改动，最后一次验证状态：{status}。"
            "继续之前先确认文件当前内容；涉及实现时先跑验证，"
            "不要把上一步当作已完成。"
        )
        if paths:
            notice += " 上次运行修改过的文件：" + "、".join(paths[:10])
            if len(paths) > 10:
                notice += f" 等 {len(paths)} 个"
        if journal_notice:
            notice += f" {journal_notice}"
        return notice

    async def checkpoints(
        self, thread_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """列出会话的 checkpoint（新的在前），供 fork/rewind 选择落点。

        直读 checkpointer 的 `alist`，不建图：列个历史不该要求 API Key
        或可用的沙箱（与 `history()` 同一考虑）。
        """
        saver = await self._checkpointer()
        config = {"configurable": {"thread_id": thread_id}}
        found: list[dict[str, Any]] = []
        async for item in saver.alist(config, limit=limit or None):
            checkpoint = getattr(item, "checkpoint", None) or {}
            metadata = getattr(item, "metadata", None) or {}
            values = checkpoint.get("channel_values") or {}
            preview = ""
            for message in reversed(values.get("messages") or []):
                if isinstance(message, (HumanMessage, AIMessage)):
                    text = text_of(message).strip()
                    if text:
                        preview = " ".join(text.split())[:120]
                        break
            found.append(
                {
                    "checkpoint_id": str(checkpoint.get("id", "")),
                    "step": int(metadata.get("step", 0) or 0),
                    "created_at": str(metadata.get("created_at", "")),
                    "preview": preview,
                }
            )
            if limit > 0 and len(found) >= limit:
                break
        return found

    async def _resolve_snapshot(
        self,
        graph: Any,
        thread_id: str,
        *,
        checkpoint_id: str = "",
        steps: int = 1,
    ) -> Any:
        config = {"configurable": {"thread_id": thread_id}}
        if not checkpoint_id:
            if steps <= 0:
                return await graph.aget_state(config)
            index = 0
            async for snapshot in graph.aget_state_history(config):
                if index == steps:
                    return snapshot
                index += 1
            raise ValueError(
                f"会话 {thread_id} 没有第 {steps} 个历史 checkpoint。"
            )
        async for snapshot in graph.aget_state_history(config):
            found = (
                (getattr(snapshot, "config", {}) or {})
                .get("configurable", {})
                .get("checkpoint_id", "")
            )
            if str(found) == checkpoint_id:
                return snapshot
        raise ValueError(f"找不到 checkpoint：{checkpoint_id}")

    async def fork(
        self,
        thread_id: str,
        *,
        checkpoint_id: str = "",
        new_thread_id: str = "",
    ) -> str:
        """从当前或指定 checkpoint 分叉出一个新会话，返回新 thread_id。

        分叉复制的是**状态值**（消息、计划等），原会话不受影响。
        """
        graph = await self._ensure_graph()
        source = await self._resolve_snapshot(
            graph, thread_id, checkpoint_id=checkpoint_id, steps=0
        )
        target = new_thread_id.strip() or (
            f"{thread_id}-fork-{uuid4().hex[:8]}"
        )
        await graph.aupdate_state(
            {"configurable": {"thread_id": target}},
            dict(getattr(source, "values", None) or {}),
            as_node=None,
        )
        return target

    async def compact_thread(
        self,
        thread_id: str,
        *,
        keep_recent: int = 6,
        new_thread_id: str = "",
    ) -> str:
        """把较早的对话压成一条确定性摘要，并分叉出一个新会话。

        不原地删除消息：原 thread 完整保留。新 thread 只带摘要 + 最近若干轮
        **纯文本**消息（不含 tool_calls / ToolMessage），因此不会破坏
        `tool_calls ↔ tool_call_id` 配对。
        """
        graph = await self._ensure_graph()
        source = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        values = dict(getattr(source, "values", None) or {})
        messages = list(values.get("messages") or [])
        summary = summarize_messages(
            messages,
            max_chars=max(500, int(self.settings.session_brief_chars or 4000)),
        )
        keep = max(1, min(int(keep_recent), 50))
        recent: list[Any] = []
        human_count = 0
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                human_count += 1
                if human_count > keep:
                    break
                recent.append(HumanMessage(content=text_of(message)))
            elif (
                isinstance(message, AIMessage)
                and not getattr(message, "tool_calls", None)
            ):
                text = text_of(message)
                if text:
                    recent.append(AIMessage(content=text))
        recent.reverse()
        # 新会话必须从用户消息开始；孤立的前置 AI 文本没有上下文意义。
        while recent and not isinstance(recent[0], HumanMessage):
            recent.pop(0)

        summary_text = (
            "【会话压缩摘要】\n" + summary
            if summary
            else "【会话压缩摘要】原会话没有可提炼的文本。"
        )
        new_messages: list[Any] = [HumanMessage(content=summary_text), *recent]
        target = new_thread_id.strip() or (
            f"{thread_id}-compact-{uuid4().hex[:8]}"
        )
        reset: dict[str, Any] = {
            "messages": new_messages,
            "request": "",
            "plan": [],
            "plan_kinds": [],
            "step_idx": 0,
            "plan_degraded": False,
            "replan_count": 0,
            "tweak_count": 0,
            "tool_rounds": 0,
            "budget_exhausted": False,
            "dirty": False,
            "run_dirty": False,
            "pending_verification": False,
            "empty_step_nudged": False,
            "verification": {},
            "review": {},
            "review_watermark": "",
            "retry": 0,
            "same_failure_count": 0,
            "last_failure_signature": "",
            "last_verification": {},
            "stale_paths": [],
            "approvals": {},
            "run_status": "finished",
            "session_brief": "",
            "recovery_notice": "",
            "project_instructions": "",
            "memories": [],
            # 编排/委派/steering 都是 run 级状态，不能从原 thread 带过来。
            "transient_retries": 0,
            "delegate_calls": 0,
            "delegation_allowed": False,
            "delegation_budget": 0,
            "execution_mode": "direct",
            "workflow": "",
            "workflow_reason": "",
            "repo_summary": {},
            "prefetched_tasks": [],
            "prefetched_degraded": False,
            "tasks": [],
            "task_status": {},
            "task_attempts": {},
            "worker_results": [],
            "orchestrator_round": 0,
            "orchestrator_degraded": False,
            "orchestrator_summary": "",
            "orchestrator_completed": 0,
            "orchestrator_failed": 0,
            "worker_context": "",
            "orchestration_handoff": {},
            "task_extensions_seen": 0,
            "task_extensions_applied": [],
            "task_extension_errors": [],
            "steering_cancelled": False,
            "steering_replan": False,
            "steering_replans": 0,
            "steering_applied": [],
            "workflow_revised_reason": "",
        }
        values.update(reset)
        await graph.aupdate_state(
            {"configurable": {"thread_id": target}},
            values,
            as_node=None,
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.COMPACT,
                thread_id=thread_id,
                detail=truncate(
                    f"compact -> {target}；保留最近 {keep} 轮，"
                    f"摘要 {len(summary)} 字符"
                ),
            )
        )
        return target

    async def rewind(
        self,
        thread_id: str,
        *,
        checkpoint_id: str = "",
        steps: int = 1,
    ) -> str:
        """把会话回退到更早的 checkpoint（默认退一步），返回新 checkpoint id。

        实现方式是**在回退点上新建一个分支**，历史记录不会被删除 ——
        误回退时仍可通过 checkpoint id 找回来。
        """
        graph = await self._ensure_graph()
        target = await self._resolve_snapshot(
            graph, thread_id, checkpoint_id=checkpoint_id, steps=steps
        )
        updated = await graph.aupdate_state(
            target.config, {}, as_node=None
        )
        return str(
            (updated or {}).get("configurable", {}).get("checkpoint_id", "")
        )

    async def _prune_checkpoints(self, thread_id: str) -> None:
        """按 `checkpoint_keep` 轮转当前 thread 的 checkpoint。

        失败只影响磁盘占用，不能影响已经完成的这一轮 —— 因此静默吞掉。
        """
        keep = int(getattr(self.settings, "checkpoint_keep", 0) or 0)
        if keep <= 0 or self._store is None or not self._store.persistent:
            return
        try:
            await self._store.aprune(thread_id, keep)
        except Exception:  # noqa: BLE001 - 轮转是维护动作，不阻断会话
            pass

    @property
    def persistent(self) -> bool:
        """会话是否跨进程可恢复。"""
        return self._store is not None and self._store.persistent

    async def aclose(self) -> None:
        """释放 sqlite 连接。显式传入 checkpointer 时由调用方负责。

        前端可以不调用：每次写入都已 commit，进程退出不会丢已落盘的 checkpoint。
        """
        self._release_workspace_lock()
        if self._execution is not None:
            self._execution.close()
            self._execution = None
        if self._store is not None:
            await self._store.aclose()
            self._graph = None

    def _forget_thread(self, thread_id: str) -> None:
        """运行收尾后清掉按 thread_id 累积的状态。

        `_pending_calls` / `_progress` 只为「挂起与恢复是两次 `_stream`」而存在
        （见 `__init__` 注释），一旦这轮以 RunFinished / RunFailed 收场就再无用处。
        长会话（TUI 一个进程跑几十轮）里不清会一直累积，而且 `_pending_calls`
        留着还会让下一轮误以为有旧的待配对调用。

        `_resume_counts` **故意不清**：它封顶的是「同一 thread 反复挂起-恢复」，
        跑完一轮就清零等于把护栏拆了（前端跑完再 resume 就能无限续）。它每
        thread 只占一个 int，本来也不是内存问题的所在。
        """
        self._pending_calls.pop(thread_id, None)
        self._progress.pop(thread_id, None)
        # 一轮结束：未消费的迟到指令与暂停标记都作废，但丢弃动作留审计。
        discarded = self.steering.clear(thread_id)
        if discarded:
            self._audit_discarded_steering(thread_id, discarded, "run 结束")
