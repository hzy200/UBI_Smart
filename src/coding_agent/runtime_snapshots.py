"""`AgentRuntime` 的**文件快照 / 回滚 / 差异**部分（mixin）。

为什么拆出来：`runtime.py` 3,131 行里绝大多数是编排与事件翻译。这一块
（留底、列快照、回滚、差异、用户上传留底）跟它们只有"共用 self 上的
settings / sandbox / audit"这一层耦合，塞在一起只让"改一行要读三千行"更严重。

**这是 mixin，不是新抽象**：它没有自己的 `__init__`，不持有状态，只依赖宿主
已经提供的属性（见下面的类型声明）。宿主是 `AgentRuntime(SnapshotMixin)`，
方法查找路径不变，调用方一行都不用改。

不变量（动了会破坏验收锚点，见 `docs/ACCEPTANCE.md`）：

- 回滚是"先留底再覆盖"，所以整次回滚**也可再次撤销**；
- 回滚失败同样要记审计 —— 用户以为回滚了但没回滚，审计里不能留空白；
- 上传是**用户动作**，不经模型写权限，但路径仍走 `SandboxFs` 校验。
"""

from __future__ import annotations

import posixpath
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from coding_agent.audit import AuditRecord
from coding_agent.audit import models as audit_models
from coding_agent.audit.logger import (
    audit_files_by_day,
    now_iso,
    read_records_many,
    truncate,
)
from coding_agent.diffing import unified_diff
from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.snapshots import (
    ACTION_RESTORE,
    RestoreResult,
    SnapshotEntry,
    SnapshotStore,
)


class SnapshotMixin:
    """文件快照相关的方法集；只被 `AgentRuntime` 继承。"""

    # 宿主（`AgentRuntime`）提供的状态，本 mixin 一个都不自己造：
    #   settings · audit · workspace · workspace_kind · host_snapshots
    #   _sandbox · _fs · _snapshots
    # 以及宿主自己的 `_audit()`。

    def _sandbox_fs(self) -> SandboxFs:
        if self._fs is None:
            self._fs = SandboxFs(self._sandbox, self.workspace)
        return self._fs

    @property
    def snapshots(self) -> SnapshotStore:
        """文件快照存储，供 `/undo` `/diff` 使用。"""
        if self.workspace_kind == "host" and self.host_snapshots is not None:
            return self.host_snapshots
        if self._snapshots is None:
            self._snapshots = SnapshotStore(
                self._sandbox,
                self._sandbox_fs(),
                self.workspace,
                manifest_root=self.settings.resolved_snapshot_manifest_dir,
            )
        return self._snapshots

    def list_snapshots(self, *, limit: int = 20) -> list[SnapshotEntry]:
        return self.snapshots.list(limit=limit)

    def prune_snapshots(self) -> list[str]:
        """按配置清理旧快照；host 实现没有 prune 时跳过。"""
        prune = getattr(self.snapshots, "prune", None)
        if not callable(prune):
            return []
        try:
            return list(
                prune(
                    keep=int(self.settings.snapshot_keep_count or 0),
                    max_age_days=int(self.settings.snapshot_keep_days or 0),
                )
            )
        except Exception:  # noqa: BLE001 - 清理失败不阻断运行
            return []

    def restore(
        self, *, path: str | None = None, snapshot_id: str | None = None
    ) -> RestoreResult:
        """回滚文件并记审计。"""
        store = self.snapshots
        entry: SnapshotEntry | None = None

        # 解析失败同样要记账：用户以为回滚了但没回滚，审计里不能留空白
        def _failed(message: str) -> RestoreResult:
            result = RestoreResult(ok=False, path=path or "", message=message)
            self._audit_rollback(result, snapshot_id or "")
            return result

        if snapshot_id:
            entry = store.find(snapshot_id, path)
            if entry is None:
                return _failed(f"找不到快照 {snapshot_id}")
        elif path:
            entry = store.latest_for(path)
            if entry is None:
                return _failed(f"{path} 没有任何留底，无法回滚。")
        else:
            entries = store.list(limit=1)
            if not entries:
                return _failed("工作区里还没有任何快照，无法回滚。")
            entry = entries[0]

        result = store.restore(entry)
        self._audit_rollback(result, snapshot_id or "")
        return result

    @staticmethod
    def _snapshot_prefix(ts: str) -> str:
        """把审计里的 ISO 时间转成快照 id 的可比较前缀。"""
        try:
            moment = datetime.fromisoformat(ts)
        except ValueError:
            return ""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S%f")

    def last_run_started_at(self, thread_id: str | None = None) -> str:
        """最近一次 `run_start` 的时间戳（优先按 thread 过滤）。"""
        groups = audit_files_by_day(self.audit.directory)
        for _day, paths in sorted(groups.items(), reverse=True):
            records = read_records_many(paths, thread_id=thread_id, limit=0)
            for record in reversed(records):
                if record.kind != audit_models.RUN_START:
                    continue
                if (
                    not thread_id
                    and record.workspace
                    and record.workspace != self.workspace
                ):
                    continue
                return record.ts
        return ""

    def restore_since(
        self, prefix: str, *, dry_run: bool = False
    ) -> list[RestoreResult]:
        """回滚某个时间点之后产生的所有快照（按新→旧）。

        `dry_run` 只列出将被回滚的文件，不写盘。真正回滚时每个文件仍走
        `SnapshotStore.restore`：先给当前内容留底，所以整次回滚也可再次撤销。
        """
        if not prefix:
            return []
        entries = [
            entry for entry in self.snapshots.list() if entry.snapshot_id > prefix
        ]
        results: list[RestoreResult] = []
        for entry in entries:
            if dry_run:
                results.append(
                    RestoreResult(
                        ok=True,
                        path=entry.path,
                        snapshot_id=entry.snapshot_id,
                        message="dry-run：未修改",
                    )
                )
                continue
            result = self.snapshots.restore(entry)
            self._audit_rollback(result, entry.snapshot_id)
            results.append(result)
        return results

    def session_patch(self, thread_id: str) -> str:
        """把某会话**本轮**的改动合成一份 unified diff（可直接 `git apply`）。

        同一文件可能有多次写前快照，这里按路径只取**最早**的那份与当前内容
        比较 —— 那才是"这一轮总共改了什么"。取最新快照会漏掉第一次改动，
        全部拼接则会产生互相重叠、无法 apply 的补丁。

        本轮起点用 `last_run_started_at`：没有 run_start 时退化为"全部快照"，
        宁可给多了让人自己筛，也不要因为审计缺失就返回空补丁。
        """
        prefix = self.last_run_started_at(thread_id)
        entries = [
            entry
            for entry in self.snapshots.list()
            if not prefix or entry.snapshot_id > prefix
        ]
        earliest: dict[str, SnapshotEntry] = {}
        for entry in sorted(entries, key=lambda item: item.snapshot_id):
            earliest.setdefault(entry.path, entry)
        chunks = [
            self.diff_snapshot(snapshot_id=entry.snapshot_id)
            for entry in earliest.values()
        ]
        return "\n".join(chunk for chunk in chunks if chunk.strip())

    def _audit_rollback(self, result: RestoreResult, requested_id: str) -> None:
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.ROLLBACK,
                workspace=self.workspace,
                path=result.path,
                action=ACTION_RESTORE,
                added=result.added,
                removed=result.removed,
                snapshot_id=result.snapshot_id or requested_id or None,
                ok=result.ok,
                detail=truncate(result.message),
            )
        )

    def diff_snapshot(
        self, *, path: str | None = None, snapshot_id: str | None = None
    ) -> str:
        """展示某个快照与当前内容的差异（即「你都改了什么」）。"""
        store = self.snapshots
        entry: SnapshotEntry | None = None

        if snapshot_id:
            entry = store.find(snapshot_id, path)
        elif path:
            entry = store.latest_for(path)
        else:
            entries = store.list(limit=1)
            entry = entries[0] if entries else None

        if entry is None:
            return ""

        try:
            original = store.read(entry)
        except Exception:  # noqa: BLE001 - 快照读不出来时给空 diff，由前端提示
            return ""

        target = posixpath.join(store.root, entry.path)
        try:
            current = self._sandbox_fs().read_text(
                target, max_bytes=self.settings.max_file_read_bytes
            )
        except Exception:  # noqa: BLE001 - 文件已被删除时按空内容比较
            current = ""

        return unified_diff(original, current, entry.path)

    def store_upload(
        self,
        filename: str,
        data: bytes,
        *,
        thread_id: str = "",
        max_bytes: int = 0,
    ) -> dict[str, Any]:
        """把用户上传的文件写入工作区的 `.agent/uploads`。

        这是**用户动作**，不经过模型的写权限，也不修改仓库源码；文件路径
        仍然走 SandboxFs 的路径校验，并写审计。返回工作区内的绝对路径。
        """
        if max_bytes and len(data) > max_bytes:
            raise ValueError(f"文件超过上限 {max_bytes} 字节")
        # 只取 basename，挡住 ../ 与 Windows 反斜杠路径。
        name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not name or name in {".", ".."} or "\x00" in name:
            raise ValueError("非法文件名")
        name = name[:180]
        safe_thread = "".join(
            ch for ch in str(thread_id or "") if ch.isalnum() or ch in "_-"
        )[:64] or "global"
        rel_dir = f".agent/uploads/{safe_thread}"
        rel_path = posixpath.join(rel_dir, f"{uuid4().hex[:8]}-{name}")
        written = self._sandbox_fs().write_bytes(rel_path, data)
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.FILE_CHANGE,
                thread_id=thread_id,
                action="upload",
                path=rel_path,
                added=written,
            )
        )
        return {
            "path": posixpath.join(self.workspace, rel_path),
            "relative_path": rel_path,
            "bytes": written,
        }
