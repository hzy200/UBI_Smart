"""会话级合并补丁：按文件取最早快照，而不是把每份快照的 diff 拼起来。

拼接全部快照会产生互相重叠、无法 `git apply` 的补丁；只取最新的一份又会漏掉
第一次改动。这里是那条口径的唯一实现，所以单独钉住。
"""

from __future__ import annotations

from typing import Any

from coding_agent.runtime_snapshots import SnapshotMixin
from coding_agent.sandbox.snapshots import SnapshotEntry


class _Store:
    def __init__(self, entries: list[SnapshotEntry]) -> None:
        self._entries = entries

    def list(self, *, limit: int = 0) -> list[SnapshotEntry]:
        return list(self._entries)


class _Patchable(SnapshotMixin):
    """只为 `session_patch` 提供三个协作对象，不碰沙箱。"""

    def __init__(
        self,
        entries: list[SnapshotEntry],
        *,
        prefix: str = "",
        diffs: dict[str, str] | None = None,
    ) -> None:
        self._entries = entries
        self._prefix = prefix
        self._diffs = diffs or {}
        self.asked: list[str] = []

    @property
    def snapshots(self) -> Any:  # type: ignore[override]
        return _Store(self._entries)

    def last_run_started_at(self, thread_id: str | None = None) -> str:
        return self._prefix

    def diff_snapshot(
        self, *, path: str | None = None, snapshot_id: str | None = None
    ) -> str:
        self.asked.append(str(snapshot_id or ""))
        return self._diffs.get(str(snapshot_id or ""), "")


def test_same_file_uses_the_earliest_snapshot() -> None:
    entries = [
        SnapshotEntry("20260101T000002", "a.py"),
        SnapshotEntry("20260101T000001", "a.py"),
        SnapshotEntry("20260101T000003", "b.py"),
    ]
    runtime = _Patchable(
        entries,
        diffs={
            "20260101T000001": "--- a.py\n+++ a.py\n-x\n+y\n",
            "20260101T000003": "--- b.py\n+++ b.py\n+new\n",
        },
    )

    patch = runtime.session_patch("t1")

    assert runtime.asked == ["20260101T000001", "20260101T000003"]
    assert "+y" in patch and "+new" in patch


def test_snapshots_before_run_start_are_ignored() -> None:
    entries = [
        SnapshotEntry("20260101T000001", "old.py"),
        SnapshotEntry("20260101T000005", "new.py"),
    ]
    runtime = _Patchable(
        entries,
        prefix="20260101T000004",
        diffs={"20260101T000005": "--- new.py\n+++ new.py\n+x\n"},
    )

    patch = runtime.session_patch("t1")

    assert runtime.asked == ["20260101T000005"]
    assert "old.py" not in patch


def test_missing_run_start_falls_back_to_every_snapshot() -> None:
    """审计缺失时宁可给多，也不要返回空补丁让人以为没改动。"""
    entries = [SnapshotEntry("20260101T000001", "a.py")]
    runtime = _Patchable(
        entries, prefix="", diffs={"20260101T000001": "--- a.py\n+x\n"}
    )

    assert "+x" in runtime.session_patch("t1")


def test_empty_diffs_are_dropped() -> None:
    entries = [
        SnapshotEntry("20260101T000001", "a.py"),
        SnapshotEntry("20260101T000002", "b.py"),
    ]
    runtime = _Patchable(
        entries,
        diffs={"20260101T000001": "", "20260101T000002": "--- b.py\n+x\n"},
    )

    patch = runtime.session_patch("t1")

    assert patch == "--- b.py\n+x\n"
