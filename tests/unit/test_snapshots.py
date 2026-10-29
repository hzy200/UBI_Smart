"""快照的「新建可回滚」语义（不依赖 WSL）。

`SnapshotStore` 只有 `list()` 需要沙箱跑一次 `find`，其余全走 `SandboxFs`，
所以用内存版 fs + 假沙箱就能把回滚语义钉住 —— 这正是 B3 的修复点：

    新建的文件此前不留底 → 「回滚最近一次改动」对它根本不成立。

而新建恰恰是最想撤销的一类改动（放错位置、内容整个不对）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from coding_agent.sandbox.fs import FileStat, SandboxFsError
from coding_agent.sandbox.snapshots import SnapshotStore

ROOT = "/ws"
BACKUP_PREFIX = f"{ROOT}/.agent/backups/"


class _Result:
    def __init__(self, stdout: str) -> None:
        self.ok = True
        self.stdout = stdout
        self.stderr = ""

    def render(self, limit: int = 500) -> str:
        return self.stdout


class _FakeSandbox:
    """只为 `list()` 服务：把内存里的备份文件列成 `<snapshot_id>/<relpath>`。"""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def run(self, script: str, **_: Any) -> _Result:
        if script.startswith("rm -rf -- "):
            target = script[len("rm -rf -- "):].strip().strip("'")
            for path in list(self.files):
                if path == target or path.startswith(target + "/"):
                    self.files.pop(path, None)
            return _Result("")
        names = sorted(
            path[len(BACKUP_PREFIX) :]
            for path in self.files
            if path.startswith(BACKUP_PREFIX)
        )
        return _Result("\n".join(names) + ("\n" if names else ""))


class _FakeFs:
    """内存版 SandboxFs：只实现 SnapshotStore 用到的那几个方法。"""

    root = ROOT

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def write_text(self, path: str, content: str) -> int:
        self.files[path] = content
        return len(content.encode("utf-8"))

    def write_many(self, files: dict[str, str]) -> int:
        for path, content in files.items():
            self.write_text(path, content)
        return sum(len(value.encode("utf-8")) for value in files.values())

    def read_text(self, path: str, *, max_bytes: int) -> str:
        if path not in self.files:
            raise SandboxFsError(f"文件不存在：{path}")
        if len(self.files[path]) > max_bytes:
            raise SandboxFsError("文件过大")
        return self.files[path]

    def stat(self, path: str) -> FileStat:
        exists = path in self.files
        return FileStat(
            exists=exists,
            is_file=exists,
            size=len(self.files.get(path, "")),
            real_path=path,
        )

    def resolve(self, path: str) -> str:
        return path

    def remove(self, path: str) -> bool:
        return self.files.pop(path, None) is not None


def _store(
    files: dict[str, str] | None = None,
    manifest_root: Path | None = None,
) -> tuple[SnapshotStore, dict[str, str]]:
    files = files if files is not None else {}
    store = SnapshotStore(
        _FakeSandbox(files),  # type: ignore[arg-type]
        _FakeFs(files),  # type: ignore[arg-type]
        ROOT,
        manifest_root=manifest_root,
    )
    return store, files


# ---------------- 新建的留底 ----------------


def test_creating_a_file_leaves_a_snapshot() -> None:
    """新建也要留底 —— 否则「回滚最近一次改动」对它不成立。"""
    store, _ = _store()
    snapshot_id = store.save(f"{ROOT}/new.py", "", existed=False)

    entry = store.latest_for(f"{ROOT}/new.py")
    assert entry is not None
    assert entry.snapshot_id == snapshot_id


def test_save_many_writes_all_snapshots_in_one_batch() -> None:
    store, files = _store()

    ids = store.save_many(
        [
            (f"{ROOT}/a.py", "old a", True),
            (f"{ROOT}/new.py", "", False),
        ]
    )

    assert len(ids) == 2
    assert store.read(store.latest_for(f"{ROOT}/a.py")) == "old a"  # type: ignore[arg-type]
    absent_markers = [
        path for path in files if path.endswith(".absent")
    ]
    assert absent_markers


def test_absent_marker_is_not_a_snapshot_entry() -> None:
    """「当时不存在」的标记只是附注，不能混进快照列表。"""
    store, _ = _store()
    store.save(f"{ROOT}/new.py", "", existed=False)

    assert [e.path for e in store.list()] == ["new.py"]


def test_overwrite_snapshot_is_not_marked_absent() -> None:
    store, _ = _store()
    store.save(f"{ROOT}/a.py", "old\n")

    entry = store.latest_for(f"{ROOT}/a.py")
    assert entry is not None
    assert store.was_absent(entry) is False


# ---------------- 回滚语义 ----------------


def test_rolling_back_a_creation_deletes_the_file() -> None:
    """回滚一次新建 = 删掉它。还原成空文件只是留下一堆删不掉的垃圾。"""
    files = {f"{ROOT}/new.py": "hello\n"}
    store, files = _store(files)
    store.save(f"{ROOT}/new.py", "", existed=False)

    entry = store.latest_for(f"{ROOT}/new.py")
    assert entry is not None
    result = store.restore(entry)

    assert result.ok is True
    assert f"{ROOT}/new.py" not in files, "回滚新建必须删掉文件，而不是清空它"
    assert "已删除" in result.message


def test_deleting_rollback_is_itself_rollbackable() -> None:
    """删之前先留底，所以这次删除也能再回滚。"""
    files = {f"{ROOT}/new.py": "hello\n"}
    store, files = _store(files)
    store.save(f"{ROOT}/new.py", "", existed=False)

    first = store.restore(store.latest_for(f"{ROOT}/new.py"))  # type: ignore[arg-type]
    assert first.undo_snapshot_id

    second = store.restore(store.latest_for(f"{ROOT}/new.py"))  # type: ignore[arg-type]
    assert second.ok is True
    assert files[f"{ROOT}/new.py"] == "hello\n"


def test_an_empty_file_is_restored_as_empty_not_deleted() -> None:
    """判别性用例：原来就是空文件 → 还原成空文件，不能删掉。

    没有 `.absent` 标记就分不清这两种情形，这条用例守的就是这个区分。
    """
    files = {f"{ROOT}/e.py": "x\n"}
    store, files = _store(files)
    store.save(f"{ROOT}/e.py", "")  # existed 默认 True：改前是个空文件

    entry = store.latest_for(f"{ROOT}/e.py")
    assert entry is not None
    assert store.was_absent(entry) is False

    result = store.restore(entry)
    assert result.ok is True
    assert files[f"{ROOT}/e.py"] == ""
    assert "已回滚" in result.message


def test_manifest_records_hashes_and_absent_flags(tmp_path: Path) -> None:
    store, _ = _store(manifest_root=tmp_path)
    first = store.save(f"{ROOT}/a.py", "old")
    second = store.save(f"{ROOT}/new.py", "", existed=False)

    manifests = {
        json.loads(path.read_text(encoding="utf-8"))["snapshot_id"]: path
        for path in tmp_path.glob("*/*.json")
    }
    assert set(manifests) == {first, second}
    first_data = json.loads(manifests[first].read_text(encoding="utf-8"))
    assert first_data["files"]["a.py"]["sha256"] == hashlib.sha256(
        b"old"
    ).hexdigest()
    assert first_data["files"]["a.py"]["absent"] is False
    second_data = json.loads(manifests[second].read_text(encoding="utf-8"))
    assert second_data["files"]["new.py"]["absent"] is True


def test_tampered_snapshot_is_rejected(tmp_path: Path) -> None:
    files = {f"{ROOT}/a.py": "current"}
    store, files = _store(files, manifest_root=tmp_path)
    snapshot_id = store.save(f"{ROOT}/a.py", "original")
    files[f"{BACKUP_PREFIX}{snapshot_id}/a.py"] = "tampered"

    entry = store.latest_for(f"{ROOT}/a.py")
    assert entry is not None
    result = store.restore(entry)

    assert result.ok is False
    assert "校验失败" in result.message
    assert files[f"{ROOT}/a.py"] == "current", "校验失败时不能覆盖当前文件"


def test_manifest_absent_flag_survives_deleted_marker(tmp_path: Path) -> None:
    files: dict[str, str] = {}
    store, files = _store(files, manifest_root=tmp_path)
    snapshot_id = store.save(f"{ROOT}/new.py", "", existed=False)
    entry = store.latest_for(f"{ROOT}/new.py")
    assert entry is not None

    files.pop(f"{BACKUP_PREFIX}{snapshot_id}/new.py.absent")
    assert store.was_absent(entry) is True


def test_rolling_back_a_creation_twice_is_a_no_op() -> None:
    """文件已经不在了，再说「删掉」是假的 —— 如实报无需回滚。"""
    store, _ = _store()
    store.save(f"{ROOT}/new.py", "", existed=False)

    result = store.restore(store.latest_for(f"{ROOT}/new.py"))  # type: ignore[arg-type]
    assert result.ok is True
    assert "无需回滚" in result.message
    assert result.undo_snapshot_id is None


def test_prune_keeps_newest_and_respects_age(tmp_path) -> None:
    newest = "20261028T000000000000000-aaaaaa"
    middle = "20261027T000000000000000-bbbbbb"
    oldest = "20200101T000000000000000-cccccc"
    files = {
        f"{BACKUP_PREFIX}{newest}/a.py": "1",
        f"{BACKUP_PREFIX}{middle}/a.py": "2",
        f"{BACKUP_PREFIX}{oldest}/a.py": "3",
        # 非本模块生成的目录名不能被 rm。
        f"{BACKUP_PREFIX}not-a-snapshot/a.py": "4",
    }
    store, _ = _store(files, manifest_root=tmp_path / "manifest")
    for sid in (newest, middle, oldest):
        manifest = store._manifest_path(sid)
        assert manifest is not None
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}", encoding="utf-8")

    removed = store.prune(keep=1, max_age_days=30)
    assert set(removed) == {middle, oldest}
    assert store._manifest_path(newest).exists()
    assert not store._manifest_path(middle).exists()

    # keep 足够大时，只按天数删除。
    removed = store.prune(keep=10, max_age_days=30)
    assert removed == []
    removed = store.prune(keep=0, max_age_days=30)
    assert set(removed) == {newest}
