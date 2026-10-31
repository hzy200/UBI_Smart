"""待合并补丁的落盘登记（崩溃后对账用）。

为什么需要它：`SchedulerRun` 的 `results / task_status / merge_queue`
全在内存里，而补丁在合并时已经 `git apply` 进主工作区。进程被 kill 时：

- 主工作区里留着半合并的改动（**会一直留着**）；
- 合并前留的快照还在，但"这批补丁合到哪一步"这个事实随内存一起消失，
  于是没人知道要回滚 —— 下次运行只会看到一个莫名"脏"的工作区。

这里只做三件事，不做自动回滚（工作区里可能已经有用户的手工改动，
擅自回滚会造成二次破坏）：

- `open_batch`：flush **之前**把整批补丁登记进 `.agent/merge-journal.json`；
- `close`：合并结束（成功、失败或被丢弃）后清空登记；
- `notice`：下次运行启动时读到非空登记，生成一句人话提示。

登记写在 `.agent/` 下：它已被 `.gitignore` 与补丁 pathspec 排除，
不会污染用户改动；也是文件工具默认的写拒绝区，模型碰不到。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ubismart.sandbox.fs import SandboxFs

JOURNAL_REL_PATH = ".agent/merge-journal.json"

# 登记只留"够用来对账"的字段：task_id 定位补丁，files 定位可能半改的文件。
_MAX_FILES_PER_RECORD = 20
_MAX_NOTICE_FILES = 10


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MergeJournal:
    """`MergeQueue` 的落盘影子；无沙箱/无 root 时整体降级为 no-op。"""

    def __init__(self, sandbox: Any, root: str) -> None:
        self._sandbox = sandbox
        self._root = str(root or "")

    @property
    def enabled(self) -> bool:
        return self._sandbox is not None and bool(self._root)

    def open_batch(self, records: list[dict[str, Any]]) -> None:
        """合并**开始前**登记；登记失败不阻断合并（代价只是少一条线索）。"""
        if not self.enabled:
            return
        payload = {
            "opened_at": _now(),
            "batch": [
                {
                    "task_id": str(record.get("task_id", "")),
                    "patch_path": str(record.get("patch_path") or ""),
                    "files": [
                        str(item)
                        for item in (record.get("files") or [])
                        if str(item)
                    ][:_MAX_FILES_PER_RECORD],
                }
                for record in records
                if isinstance(record, dict)
            ],
        }
        try:
            SandboxFs(self._sandbox, self._root).write_text(
                JOURNAL_REL_PATH, json.dumps(payload, ensure_ascii=False)
            )
        except Exception:  # noqa: BLE001 - 登记是 best-effort：写不出来
            # 不能阻断合并，代价只是崩溃后少一条线索。
            return

    def close(self) -> None:
        """合并已收尾（成功/失败/丢弃）：清空登记。"""
        if not self.enabled:
            return
        try:
            SandboxFs(self._sandbox, self._root).write_text(
                JOURNAL_REL_PATH, "{}"
            )
        except Exception:  # noqa: BLE001 - 同上：清理登记失败不影响合并
            return

    def load(self) -> list[dict[str, Any]]:
        """读取未收尾的登记；缺失或损坏一律当空（不能因为坏文件挡住运行）。"""
        if not self.enabled:
            return []
        try:
            raw = SandboxFs(self._sandbox, self._root).read_text(
                JOURNAL_REL_PATH, max_bytes=64 * 1024
            )
        except Exception:  # noqa: BLE001 - 读不出来当没有登记，不能挡住运行
            return []
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        batch = payload.get("batch")
        if not isinstance(batch, list):
            return []
        return [item for item in batch if isinstance(item, dict)]

    def notice(self) -> str:
        """给模型与用户的提示；没有残留时返回空串。"""
        pending = self.load()
        if not pending:
            return ""
        task_ids = [
            str(item.get("task_id", ""))
            for item in pending
            if str(item.get("task_id", ""))
        ]
        files = sorted(
            {
                str(path)
                for item in pending
                for path in (item.get("files") or [])
                if str(path)
            }
        )
        text = (
            "上一次运行的并发补丁合并没有正常收尾（进程可能在合并中途被中断），"
            f"未确认完成的任务：{'、'.join(task_ids[:5]) or '未知'}。"
            "工作区可能停留在半合并状态。"
        )
        if files:
            text += " 涉及文件：" + "、".join(files[:_MAX_NOTICE_FILES])
            if len(files) > _MAX_NOTICE_FILES:
                text += f" 等 {len(files)} 个"
        text += " 继续之前先用 `agent diff` 确认当前内容，必要时 `agent undo --last-run` 回滚。"
        return text
