"""待合并补丁的落盘登记。

崩溃时内存里的 `results / task_status / merge_queue` 全没了，只有这份登记
能说明"上次合并到哪一步"。所以它必须：合并**前**写、收尾后清、坏了当没有。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ubismart.orchestrator import merge_journal as journal_module
from ubismart.orchestrator.merge_journal import MergeJournal
from ubismart.orchestrator.merge_queue import MergeQueue


class _FakeFs:
    """内存版 SandboxFs：`write_text` / `read_text` 与真实实现同签名。"""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def write_text(self, path: str, content: str) -> int:
        self.files[str(path)] = content
        return len(content)

    def read_text(self, path: str, *, max_bytes: int) -> str:
        if str(path) not in self.files:
            raise OSError(f"no such file: {path}")
        return self.files[str(path)]


@pytest.fixture()
def fake_fs(monkeypatch) -> dict[str, str]:
    """把模块里的 `SandboxFs` 换成内存实现，返回那份"沙盘文件"。"""
    store: dict[str, str] = {}
    monkeypatch.setattr(
        journal_module, "SandboxFs", lambda *_a, **_k: _FakeFs(store)
    )
    return store


def _journal() -> MergeJournal:
    return MergeJournal(object(), "/ws")


def test_open_then_close_leaves_nothing(fake_fs: dict[str, str]) -> None:
    journal = _journal()
    journal.open_batch(
        [{"task_id": "t1", "patch_path": "/ws/.agent/patches/t1.patch",
          "files": ["a.py", "b.py"]}]
    )
    assert journal.load()
    assert "t1" in journal.notice()

    journal.close()
    assert journal.load() == []
    assert journal.notice() == ""


def test_notice_lists_tasks_and_files(fake_fs: dict[str, str]) -> None:
    journal = _journal()
    journal.open_batch(
        [
            {"task_id": "t1", "patch_path": "/p1", "files": ["a.py"]},
            {"task_id": "t2", "patch_path": "/p2", "files": ["b.py"]},
        ]
    )
    notice = journal.notice()
    assert "t1" in notice and "t2" in notice
    assert "a.py" in notice and "b.py" in notice
    assert "agent undo" in notice


def test_corrupt_or_missing_journal_is_treated_as_empty(
    fake_fs: dict[str, str],
) -> None:
    journal = _journal()
    assert journal.load() == []

    fake_fs[journal_module.JOURNAL_REL_PATH] = "{ not json"
    assert journal.load() == []

    fake_fs[journal_module.JOURNAL_REL_PATH] = json.dumps(["not-a-dict"])
    assert journal.load() == []


def test_disabled_journal_is_a_noop() -> None:
    for journal in (MergeJournal(None, "/ws"), MergeJournal(object(), "")):
        assert journal.enabled is False
        journal.open_batch([{"task_id": "t1"}])
        journal.close()
        assert journal.load() == []
        assert journal.notice() == ""


def test_flush_registers_before_and_clears_after(
    fake_fs: dict[str, str],
) -> None:
    """登记必须在 apply 之前落下：崩溃发生在合并中途时只剩它当证据。"""
    events: list[str] = []

    class _RecordingJournal(MergeJournal):
        def open_batch(self, records):  # noqa: ANN001, ANN202
            events.append(f"open:{len(records)}")
            super().open_batch(records)

        def close(self) -> None:
            events.append("close")
            super().close()

    async def _fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        events.append("merge")
        # 合并进行中必须能看到登记已经落盘。
        assert _journal().load()
        return {
            str(record["task_id"]): (True, "", ["snap"]) for record in records
        }

    import ubismart.orchestrator.merge_queue as queue_module

    original = queue_module.merge_implement_patches_atomic
    queue_module.merge_implement_patches_atomic = _fake_merge
    try:
        queue: MergeQueue = MergeQueue(_RecordingJournal(object(), "/ws"))
        finalized: list[dict] = []
        queue.add({"task_id": "t1", "files": ["a.py"]})

        merged = asyncio.run(
            queue.flush(
                sandbox=object(),
                root="/ws",
                allow_overlap=False,
                finalize=finalized.append,
            )
        )
    finally:
        queue_module.merge_implement_patches_atomic = original

    assert merged is True
    assert events == ["open:1", "merge", "close"]
    assert _journal().load() == []


def test_merge_exception_keeps_registration(
    fake_fs: dict[str, str], monkeypatch
) -> None:
    async def _broken_merge(*_a, **_k):  # noqa: ANN002, ANN003
        raise RuntimeError("apply 中途失败")

    import ubismart.orchestrator.merge_queue as queue_module

    monkeypatch.setattr(
        queue_module, "merge_implement_patches_atomic", _broken_merge
    )
    queue = MergeQueue(_journal())
    queue.add({"task_id": "t1", "files": ["a.py"]})

    with pytest.raises(RuntimeError):
        asyncio.run(
            queue.flush(
                sandbox=object(),
                root="/ws",
                allow_overlap=False,
                finalize=lambda _record: None,
            )
        )
    # 未完整收尾：登记必须留着给下次启动对账。
    assert [item["task_id"] for item in _journal().load()] == ["t1"]


def test_cancelled_merge_keeps_registration(
    fake_fs: dict[str, str], monkeypatch
) -> None:
    async def _cancelled_merge(*_a, **_k):  # noqa: ANN002, ANN003
        raise asyncio.CancelledError

    import ubismart.orchestrator.merge_queue as queue_module

    monkeypatch.setattr(
        queue_module, "merge_implement_patches_atomic", _cancelled_merge
    )
    queue = MergeQueue(_journal())
    queue.add({"task_id": "t1", "files": ["a.py"]})

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            queue.flush(
                sandbox=object(),
                root="/ws",
                allow_overlap=False,
                finalize=lambda _record: None,
            )
        )
    assert [item["task_id"] for item in _journal().load()] == ["t1"]


def test_finalize_failure_keeps_registration(
    fake_fs: dict[str, str], monkeypatch
) -> None:
    async def _fake_merge(sandbox, root, records, **_kwargs):  # noqa: ANN001
        return {
            str(record["task_id"]): (True, "", ["snap"])
            for record in records
        }

    import ubismart.orchestrator.merge_queue as queue_module

    monkeypatch.setattr(
        queue_module, "merge_implement_patches_atomic", _fake_merge
    )
    queue = MergeQueue(_journal())
    queue.add({"task_id": "t1", "files": ["a.py"]})

    def _broken_finalize(_record: dict) -> None:
        raise RuntimeError("finalize 失败")

    with pytest.raises(RuntimeError):
        asyncio.run(
            queue.flush(
                sandbox=object(),
                root="/ws",
                allow_overlap=False,
                finalize=_broken_finalize,
            )
        )
    assert [item["task_id"] for item in _journal().load()] == ["t1"]


def test_discard_keeps_registration_and_issues_no_io(
    fake_fs: dict[str, str],
) -> None:
    """丢弃路径没做过登记，也不能清掉上次崩溃留下的现场。"""
    journal = _journal()
    journal.open_batch([{"task_id": "t0", "files": ["c.py"]}])
    queue = MergeQueue(journal)
    queue.add({"task_id": "t1"})
    queue.discard("用户取消，未合并", lambda record: None)
    assert [item["task_id"] for item in journal.load()] == ["t0"]
