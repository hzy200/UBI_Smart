"""长期记忆：项目/全局记忆的读写、置顶、优先级与审计。

从 `core.py` 拆出，只做职责归类；方法体未改动。
"""

from __future__ import annotations

from ubismart.audit import AuditRecord
from ubismart.audit import models as audit_models
from ubismart.audit.logger import now_iso, truncate
from ubismart.memory.longterm import (
    LayeredMemory,
    LongTermMemory,
    resolve_global_memory_root,
    resolve_memory_root,
)
from ubismart.memory.semantic import SemanticMemory, wrap_memory
from ubismart.sandbox.fs import SandboxFs


class MemoryMixin:
    """`AgentRuntime` 的长期记忆。"""

    def _memory_root(self) -> str:
        return resolve_memory_root(
            self.settings, self._sandbox, self.workspace
        )

    def _global_memory_root(self) -> str:
        return resolve_global_memory_root(self.settings, self._sandbox)

    def _ensure_memory_bind(self) -> None:
        """记忆在仓库外时，bwrap 需要显式可写绑定。"""
        roots = [self._memory_root()]
        global_root = self._global_memory_root()
        if global_root and global_root not in roots:
            roots.append(global_root)
        for root in roots:
            if root == self.workspace:
                continue
            self._sandbox.ensure_directory(root)
            self._sandbox.add_extra_bind(root)

    @property
    def memory(self) -> SemanticMemory:
        """项目记忆 + 可选全局记忆 + 可选向量检索。"""
        if self._memory is None:
            self._ensure_memory_bind()
            memory_root = self._memory_root()
            project = LongTermMemory(
                SandboxFs(self._sandbox, memory_root),
                self.workspace,
                state_root=memory_root,
            )
            global_memory = None
            global_root = self._global_memory_root()
            if global_root:
                global_memory = LongTermMemory(
                    SandboxFs(self._sandbox, global_root),
                    global_root,
                    state_root=global_root,
                )
            self._memory = wrap_memory(
                LayeredMemory(project, global_memory), self.settings
            )
        return self._memory

    def _audit_memory(
        self, thread_id: str, detail: str, *, source: str = "user"
    ) -> None:
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.MEMORY,
                thread_id=thread_id,
                detail=truncate(detail),
                source=source,
            )
        )

    def remember(
        self,
        text: str,
        *,
        thread_id: str = "",
        reason: str = "",
        ttl_days: int | None = None,
        scope: str = "project",
    ) -> list[str]:
        facts = self.memory.add(
            text,
            source="user",
            reason=reason,
            thread_id=thread_id,
            ttl_days=ttl_days,
            scope=scope,
        )
        detail = f"add[{scope}]: {text}"
        if reason:
            detail += f"；原因: {reason}"
        if self.memory.last_replaced:
            detail += "；替换: " + "；".join(self.memory.last_replaced)
        self._audit_memory(thread_id, detail)
        return facts

    def forget(
        self, index: int, *, thread_id: str = "", scope: str = "project"
    ) -> list[str]:
        facts = self.memory.remove(index, scope=scope)
        removed = self.memory.last_removed
        self._audit_memory(
            thread_id,
            f"forget[{scope}] #{index}: {removed}"
            if removed
            else f"forget[{scope}] #{index}",
        )
        return facts

    def restore_memory(
        self,
        backup_id: str,
        *,
        thread_id: str = "",
        scope: str = "project",
    ) -> list[str]:
        facts = self.memory.restore(backup_id, scope=scope)
        self._audit_memory(thread_id, f"restore[{scope}]: {backup_id}")
        return facts

    def pin_memory(
        self,
        index: int,
        *,
        pinned: bool,
        thread_id: str = "",
        scope: str = "project",
    ) -> list[str]:
        facts = self.memory.set_pinned(index, pinned, scope=scope)
        self._audit_memory(
            thread_id,
            f"{'pin' if pinned else 'unpin'}[{scope}] #{index}",
        )
        return facts

    def set_memory_priority(
        self,
        index: int,
        priority: int,
        *,
        thread_id: str = "",
        scope: str = "project",
    ) -> list[str]:
        facts = self.memory.set_priority(index, priority, scope=scope)
        self._audit_memory(
            thread_id, f"priority[{scope}] #{index}={priority}"
        )
        return facts

    def clear_memory(
        self, *, thread_id: str = "", scope: str = "project"
    ) -> list[str]:
        facts = self.memory.clear(scope=scope)
        self._audit_memory(thread_id, f"clear[{scope}]")
        return facts

    def memories(self, *, scope: str | None = None) -> list[str]:
        return self.memory.load(scope=scope)
