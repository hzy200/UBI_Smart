"""implement 补丁的待合并批次。

并发 worker 各自产出 patch，但主工作区的 `git apply` 必须串行且尽量
按批原子合并。`MergeQueue` 只负责「攒批 / 判断能否 flush / 丢弃」，
真正的校验、留底与应用仍在 `merge.py`。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from coding_agent.orchestrator.merge import merge_implement_patches_atomic
from coding_agent.orchestrator.merge_journal import MergeJournal

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

    async def flush(
        self,
        *,
        sandbox: Any,
        root: str,
        allow_overlap: bool,
        finalize: Finalize,
    ) -> bool:
        """原子合并当前批次；返回是否真的有补丁落进主工作区。"""
        if not self._pending:
            return False
        batch = list(self._pending)
        self._pending.clear()
        if sandbox is None or not root:
            for record in batch:
                finalize(record)
            return False

        # 登记必须在 apply 之前：崩溃发生在合并中途时，主工作区里可能已经
        # 有半个补丁，而没有登记就无从知道要回滚什么。
        if self._journal is not None:
            self._journal.open_batch(batch)
        try:
            outcomes = await merge_implement_patches_atomic(
                sandbox,
                root,
                batch,
                allow_overlap=allow_overlap,
            )
        finally:
            # 走到这里说明合并已收尾（成功/失败都算），无论有没有崩。
            # 崩溃发生在 apply 之间的话登记就留着，下次启动会报出来。
            if self._journal is not None:
                self._journal.close()

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
        return merged_any
