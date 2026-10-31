"""审计写入职责：把运行、工具调用、子智能体与文件改动记进 JSONL 审计链。

从 `core.py` 拆出，只做职责归类；方法体未改动。
"""

from __future__ import annotations

from typing import Any

from ubismart.audit import AuditRecord
from ubismart.audit import models as audit_models
from ubismart.audit.logger import now_iso, sanitize_args, truncate
from ubismart.events import FileChanged, ToolCallFinished, ToolCallStarted
from ubismart.sandbox.policy import APPROVED, DENIED
from ubismart.tools.artifacts import SubAgentArtifact

from .common import _swallow_audit


class AuditMixin:
    """`AgentRuntime` 的审计写入职责。"""

    def _audit_discarded_steering(
        self,
        thread_id: str,
        commands: list[Any],
        reason: str,
    ) -> None:
        """记录被丢弃的 steering 指令，避免“提交过但没生效”无迹可查。"""
        detail = "、".join(
            f"{getattr(item, 'action', '')}"
            + (f": {getattr(item, 'text', '')}" if getattr(item, "text", "") else "")
            for item in commands
        )
        try:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.STEERING,
                    thread_id=thread_id,
                    detail=truncate(f"{reason}，丢弃指令：{detail}"),
                    source="runtime",
                )
            )
        except Exception as exc:  # noqa: BLE001 - 提交时已审计，清理审计不阻断收尾
            _swallow_audit("audit_discarded_steering", exc)

    def _audit(self, record: AuditRecord) -> None:
        """写一条审计记录。

        写失败会抛 AuditError 并被上层转成 RunFailed —— 审计有缺口是这个项目
        不能接受的失败模式，宁可让运行显式失败，也不静默丢记录。
        """
        if not record.workspace:
            # run_end / tool_call 等记录默认没带 workspace；补齐后多工作区
            # 的对账才不会靠猜。解析失败不阻断审计本身。
            try:
                record.workspace = self.workspace
            except Exception:  # noqa: BLE001
                pass
        self.audit.write(record)

    def _audit_tool_call(
        self,
        started: ToolCallStarted | None,
        finished: ToolCallFinished,
        thread_id: str,
    ) -> None:
        if finished.decision == DENIED:
            decision = audit_models.DECISION_DENIED
        elif finished.decision == APPROVED:
            decision = audit_models.DECISION_APPROVED
        elif finished.rejected:
            decision = audit_models.DECISION_REJECTED
        else:
            decision = audit_models.DECISION_AUTO
        # `skipped` / `interrupted` 都是「没得出成功或失败」：审计必须记 None，
        # 不能把“没验证/结果未知”混进成功或失败率。
        audit_ok = (
            None
            if finished.interrupted or finished.skipped
            else finished.ok
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.TOOL_CALL,
                thread_id=thread_id,
                call_id=finished.call_id,
                tool=finished.name,
                args=sanitize_args(dict(started.args) if started else {}),
                level=finished.level or (started.level if started else "") or "",
                decision=decision,
                ok=audit_ok,
                exit_code=finished.exit_code,
                duration_ms=finished.duration_ms,
            )
        )

    def _audit_subagent(self, artifact: SubAgentArtifact, thread_id: str) -> None:
        """把一次子智能体运行记进审计。

        - 一条 `subagent` 汇总记录（目标、状态、摘要、token）；
        - 子智能体实际执行的每个只读工具一条 `tool_call` 记录。
          没有后者的话，主审计里只能看到一次 delegate，子智能体读了什么完全不可见。
        """
        verdict = (
            True
            if artifact.status == "success"
            else False if artifact.status == "failed" else None
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.SUBAGENT,
                thread_id=thread_id,
                agent_id=artifact.agent_id,
                parent_call_id=artifact.parent_call_id or None,
                task=truncate(artifact.goal),
                ok=verdict,
                detail=truncate(artifact.summary),
                input_tokens=artifact.input_tokens or None,
                output_tokens=artifact.output_tokens or None,
                duration_ms=artifact.duration_ms,
            )
        )
        for call in artifact.tool_calls:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.TOOL_CALL,
                    thread_id=thread_id,
                    call_id=str(call.get("call_id", "")),
                    tool=str(call.get("name", "")),
                    args=sanitize_args(dict(call.get("args") or {})),
                    level="L0 只读",
                    decision=audit_models.DECISION_AUTO,
                    ok=bool(call.get("ok")),
                    duration_ms=call.get("duration_ms"),
                    agent_id=artifact.agent_id,
                    parent_call_id=artifact.parent_call_id or None,
                )
            )

    def _audit_file_change(self, changed: FileChanged, thread_id: str) -> None:
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.FILE_CHANGE,
                thread_id=thread_id,
                path=changed.path,
                action=changed.action,
                added=changed.added,
                removed=changed.removed,
                snapshot_id=changed.snapshot_id,
            )
        )
