"""implement 补丁的待合并批次。

并发 worker 各自产出 patch，但主工作区的 `git apply` 必须串行且尽量
按批原子合并。`MergeQueue` 只负责「攒批 / 判断能否 flush / 丢弃」，
真正的校验、留底与应用仍在 `merge.py`。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ubismart.orchestrator.merge import (
    merge_implement_patches_atomic,
    select_mergeable_components,
)
from ubismart.orchestrator.merge_journal import MergeJournal

Finalize = Callable[[dict[str, Any]], None]


class MergeQueue:
    def __init__(self, journal: MergeJournal | None = None) -> None:
        self._pending: list[dict[str, Any]] = []
        # 落盘影子：合并开始前登记、收尾后清空。崩溃时它就是"合到哪了"的
        # 唯一证据（内存里的 results/status 一起没了）。
        self._journal = journal

    @property
    def pending(self) -> list[dict[str, Any]]:
        return list(self._pending)

    def __bool__(self) -> bool:
        return bool(self._pending)

    def add(self, record: dict[str, Any]) -> None:
        """加入待合并批次；真正的合并等到没有 implement 在飞时。"""
        self._pending.append(record)

    def drop(self, records: list[dict[str, Any]]) -> None:
        """移除已由调用方处理过的记录（例如审查拒绝并已 finalize）。

        不负责 finalize —— 审查路径需要先把 findings/事件写进记录再调用。
        """
        task_ids = {
            str(record.get("task_id", "")) for record in records
        }
        if not task_ids:
            return
        self._pending = [
            record
            for record in self._pending
            if str(record.get("task_id", "")) not in task_ids
        ]

    def discard(self, reason: str, finalize: Finalize) -> None:
        """取消/失败路径：未合并的补丁一律丢弃。"""
        for record in self._pending:
            record["status"] = "cancelled"
            record["merged"] = False
            record["merge_error"] = reason
            finalize(record)
        # 这里**不**动登记：`open_batch` 只在 flush 前写，丢弃路径根本没登记过；
        # 而登记里若留着上次崩溃的残留，那正是下次启动要报出来的证据，清掉它
        # 等于抹掉现场。
        self._pending.clear()

    def has_active_implement(
        self,
        *,
        in_flight: set[str],
        status: dict[str, str],
        by_id: dict[str, dict[str, Any]],
    ) -> bool:
        """是否还有 implement **正在运行**。

        只认 `in_flight`：已完成待合并、以及因并发/合并背压还没启动的
        implement 都不算。否则合并队列永远等不到 flush，背压场景下这批
        补丁还会在收尾时被整体丢弃。
        """
        return any(
            task.get("role") == "implement"
            for task_id, task in by_id.items()
            if task_id in in_flight
        )

    def take_mergeable(
        self,
        *,
        blocked_paths: set[str],
        paths_by_task: dict[str, list[str]],
    ) -> list[dict[str, Any]]:
        """取出与在飞路径不相交的完整分量；路径不全时返回空（保守回退）。"""
        if not self._pending:
            return []
        task_ids = {
            str(record.get("task_id", "")) for record in self._pending
        }
        # 任一补丁路径未知都无法证明不相交；交给调用方等整批 flush。
        if any(not paths_by_task.get(task_id) for task_id in task_ids):
            return []
        selected = select_mergeable_components(
            self._pending, paths_by_task, blocked_paths
        )
        if not selected:
            return []
        selected_ids = {
            str(record.get("task_id", "")) for record in selected
        }
        self._pending = [
            record
            for record in self._pending
            if str(record.get("task_id", "")) not in selected_ids
        ]
        return selected

    async def flush(
        self,
        *,
        sandbox: Any,
        root: str,
        allow_overlap: bool,
        finalize: Finalize,
    ) -> bool:
        """原子合并当前全部待合并批次；返回是否真有补丁落进主工作区。"""
        return await self.flush_records(
            list(self._pending),
            sandbox=sandbox,
            root=root,
            allow_overlap=allow_overlap,
            finalize=finalize,
        )

    async def flush_records(
        self,
        records: list[dict[str, Any]],
        *,
        sandbox: Any,
        root: str,
        allow_overlap: bool,
        finalize: Finalize,
    ) -> bool:
        """合并指定记录（增量批次）；未选中的记录留在 pending。"""
        if not records:
            return False
        batch = list(records)
        selected_ids = {
            str(record.get("task_id", "")) for record in batch
        }
        self._pending = [
            record
            for record in self._pending
            if str(record.get("task_id", "")) not in selected_ids
        ]
        if sandbox is None or not root:
            for record in batch:
                finalize(record)
            return False

        # 登记必须在 apply 之前：崩溃发生在合并中途时，主工作区里可能已经
        # 有半个补丁，而没有登记就无从知道要回滚什么。
        if self._journal is not None:
            self._journal.open_batch(batch)
        # 不放在 finally：合并抛异常/被取消、或 finalize 中途失败时，登记
        # 必须留着给下次启动对账；只有整批 outcomes 都落进记录后才清空。
        outcomes = await merge_implement_patches_atomic(
            sandbox,
            root,
            batch,
            allow_overlap=allow_overlap,
        )

        merged_any = False
        for record in batch:
            task_id = str(record.get("task_id", ""))
            merged, merge_error, snapshot_ids = outcomes.get(
                task_id, (False, "未合并", [])
            )
            record["merged"] = merged
            record["merge_error"] = merge_error
            record["snapshot_ids"] = snapshot_ids
            if merged:
                merged_any = True
            elif record.get("status") in {"success", "partial"}:
                record["status"] = "failed"
            finalize(record)
        if self._journal is not None:
            self._journal.close()
        return merged_any
