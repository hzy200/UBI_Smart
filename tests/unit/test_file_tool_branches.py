"""文件工具的分支覆盖：用假 FS/快照把错误与回滚路径拉进 fast 子集。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from coding_agent.config import Settings
from coding_agent.sandbox.fs import (
    FileStat,
    SandboxFsError,
    SandboxFsTooLarge,
)
from coding_agent.sandbox.pathguard import ensure_inside
from coding_agent.tools import files as files_module
from coding_agent.tools.files import _STALE_READ_MESSAGE

pytestmark = pytest.mark.regression


class _FakeFs:
    def __init__(self, files: dict[str, str] | None = None, *, dirs=None, entries=None) -> None:
        self.files = dict(files or {})
        self.dirs = set(dirs or [])
        self.entries = entries or {}
        self.cache: dict[str, bytes] = {}
        self.raises: dict[str, BaseException] = {}

    def _raise(self, name: str) -> None:
        if name in self.raises:
            raise self.raises[name]

    def lexical(self, path: str) -> str:
        return ensure_inside(path, "/ws", cwd="/ws")

    def stat(self, path: str) -> FileStat:
        self._raise("stat")
        target = self.lexical(path)
        if target in self.files:
            return FileStat(True, True, len(self.files[target]), target)
        if target in self.dirs:
            return FileStat(True, False, 0, target)
        return FileStat(False, False, 0, target)

    def read_text(self, path: str, *, max_bytes: int) -> str:
        self._raise("read_text")
        return self.files[self.lexical(path)]

    def read_for_update(self, path: str, *, max_bytes: int):
        self._raise("read_for_update")
        target = self.lexical(path)
        if target in self.files:
            data = self.files[target]
            return target, FileStat(True, True, len(data), target), data
        return target, FileStat(False, False, 0, target), None

    def cached_bytes(self, path: str) -> bytes | None:
        return self.cache.get(self.lexical(path))

    def invalidate(self, path: str) -> None:
        self.cache.pop(self.lexical(path), None)

    def write_text(self, path: str, content: str) -> int:
        self._raise("write_text")
        self.files[self.lexical(path)] = content
        return len(content.encode("utf-8"))

    def write_many(self, files: dict[str, str]) -> int:
        self._raise("write_many")
        self.files.update(files)
        return sum(len(value.encode("utf-8")) for value in files.values())

    def mkdir(self, path: str) -> str:
        self._raise("mkdir")
        target = self.lexical(path)
        self.dirs.add(target)
        return target

    def remove(self, path: str, *, known_file: bool = False) -> bool:
        self._raise("remove")
        return self.files.pop(self.lexical(path), None) is not None

    def list_dir(self, path: str, *, max_depth: int = 1, limit: int = 200):
        self._raise("list_dir")
        return self.entries.get(self.lexical(path), [])


class _FakeSnapshots:
    def __init__(self, fs: _FakeFs | None = None) -> None:
        self.fs = fs
        self.raises: set[str] = set()
        self.entry = SimpleNamespace(path="a.py")
        self.list_entries = [self.entry]
        self.restore_ok = True
        self.restore_diff = ""

    def _check(self, name: str) -> None:
        if name in self.raises:
            raise SandboxFsError(f"{name} failed")

    def write_with_snapshot(self, target, content, original="", existed=False, **kwargs) -> str:
        self._check("write_with_snapshot")
        if self.fs is not None:
            self.fs.files[target] = content
        return "snap1"

    def write_many_with_snapshots(self, entries) -> dict[str, str]:
        self._check("write_many_with_snapshots")
        if self.fs is not None:
            for target, updated, *_ in entries:
                self.fs.files[target] = updated
        return {target: "snap1" for target, *_ in entries}

    def find(self, snapshot_id: str, wanted: str | None):
        self._check("find")
        return None if wanted else self.entry

    def latest_for(self, target: str):
        self._check("latest_for")
        return self.entry

    def list(self, limit: int = 20):
        self._check("list")
        return self.list_entries

    def restore(self, entry):
        self._check("restore")
        return SimpleNamespace(
            ok=self.restore_ok,
            path=entry.path,
            message="已回滚" if self.restore_ok else "回滚失败",
            added=1,
            removed=0,
            diff=self.restore_diff,
            snapshot_id="snap1",
            undo_snapshot_id="undo1",
        )

    def move_with_snapshot(self, src, dst, src_text, dst_text, **kwargs):
        self._check("move_with_snapshot")
        if self.fs is not None:
            self.fs.files[dst] = src_text
        return "src-snap", "dst-snap"

    def save(self, target: str, original: str) -> str:
        self._check("save")
        return "snap1"


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        wsl_workspace="/ws",
        read_deny_globs=".env",
        write_deny_globs=".env",
        **overrides,
    )


def _tools(
    fs: _FakeFs,
    snapshots: _FakeSnapshots | None = None,
    *,
    write: bool = True,
    write_scope=None,
) -> dict[str, Any]:
    built = files_module.build_file_tools(
        _settings(),
        allow_write=write,
        write_scope=write_scope,
        fs=fs,
        snapshots=snapshots or _FakeSnapshots(fs),
        root="/ws",
    )
    return {tool.name: tool for tool in built}


def _text(raw: str) -> str:
    return json.loads(raw)["text"]


# ---------------- read / list ----------------


def test_read_success_and_empty_and_offset() -> None:
    fs = _FakeFs({"/ws/a.py": "one\ntwo\n"})
    read = _tools(fs)["file_read"]
    assert "共 2 行" in read.invoke({"path": "a.py", "reason": "看"})
    assert "1\tone" in _text(read.invoke({"path": "a.py", "reason": "看"}))

    fs.files["/ws/empty.py"] = ""
    assert "文件为空" in read.invoke({"path": "empty.py", "reason": "看"})
    assert "没有内容可显示" in read.invoke(
        {"path": "a.py", "reason": "看", "offset": 10}
    )


def test_read_rejects_and_error_branches() -> None:
    fs = _FakeFs({"/ws/.env": "SECRET=1\n"})
    tools = _tools(fs)
    assert "路径被拒绝" in tools["file_read"].invoke(
        {"path": "../etc/passwd", "reason": "x"}
    )
    assert "命中秘密文件读保护" in tools["file_read"].invoke(
        {"path": ".env", "reason": "x"}
    )

    fs.raises["stat"] = SandboxFsError("boom")
    assert "无法解析文件的真实路径" in tools["file_read"].invoke(
        {"path": "a.py", "reason": "x"}
    )
    del fs.raises["stat"]

    fs.raises["read_text"] = SandboxFsTooLarge("太大")
    assert "offset/limit" in tools["file_read"].invoke(
        {"path": "a.py", "reason": "x"}
    )
    fs.raises["read_text"] = RuntimeError("炸了")
    assert "炸了" in tools["file_read"].invoke({"path": "a.py", "reason": "x"})


def test_list_dir_success_empty_and_errors() -> None:
    fs = _FakeFs({}, entries={"/ws": [("d", "src"), ("f", "a.py")]})
    tools = _tools(fs)
    assert "DIR  src" in tools["file_list"].invoke({"path": ".", "reason": "看"})
    assert "（空目录）" in tools["file_list"].invoke({"path": "src", "reason": "看"})
    assert "路径被拒绝" in tools["file_list"].invoke(
        {"path": "../x", "reason": "看"}
    )

    fs.raises["list_dir"] = SandboxFsError("boom")
    assert "boom" in tools["file_list"].invoke({"path": "src", "reason": "看"})
    fs.raises["list_dir"] = RuntimeError("unexpected")
    assert "unexpected" in tools["file_list"].invoke(
        {"path": "src", "reason": "看"}
    )


# ---------------- write ----------------


def test_write_create_overwrite_stale_and_syntax() -> None:
    fs = _FakeFs()
    tools = _tools(fs)
    write = tools["file_write"]

    assert "已创建" in write.invoke({"path": "a.py", "content": "x = 1\n", "reason": "建"})
    assert "已覆盖" in write.invoke({"path": "a.py", "content": "x = 2\n", "reason": "改"})

    fs.cache["/ws/a.py"] = b"old\n"
    fs.files["/ws/a.py"] = "new\n"
    assert _STALE_READ_MESSAGE in write.invoke(
        {"path": "a.py", "content": "x = 3\n", "reason": "改"}
    )

    fs.cache.clear()
    assert "语法错误" in write.invoke(
        {"path": "a.py", "content": "def bad(:\n", "reason": "坏"}
    )


def test_write_rejects_scope_protected_and_io_errors() -> None:
    fs = _FakeFs()
    scoped = _tools(fs, write_scope=["app"])["file_write"]
    assert "write_scope" in scoped.invoke(
        {"path": "other/a.py", "content": "x\n", "reason": "越界"}
    )

    tools = _tools(fs)
    assert "路径被拒绝" in tools["file_write"].invoke(
        {"path": "../x", "content": "x\n", "reason": "越界"}
    )
    assert "敏感写保护" in tools["file_write"].invoke(
        {"path": ".env", "content": "x\n", "reason": "越界"}
    )

    fs.raises["read_for_update"] = SandboxFsTooLarge("太大")
    assert "无法读取原文件" in tools["file_write"].invoke(
        {"path": "a.py", "content": "x\n", "reason": "x"}
    )
    fs.raises["read_for_update"] = SandboxFsError("boom")
    assert "无法读取原文件" in tools["file_write"].invoke(
        {"path": "a.py", "content": "x\n", "reason": "x"}
    )


def test_write_snapshot_failure_is_reported() -> None:
    fs = _FakeFs()
    snap = _FakeSnapshots()
    snap.raises.add("write_with_snapshot")
    output = _tools(fs, snap)["file_write"].invoke(
        {"path": "a.py", "content": "x = 1\n", "reason": "写"}
    )
    assert "写入失败（已中止）" in output


# ---------------- edit ----------------


def test_edit_branches() -> None:
    fs = _FakeFs({"/ws/a.py": "one two one\n"})
    tools = _tools(fs)
    edit = tools["file_edit"]

    assert "不存在" in edit.invoke(
        {"path": "missing.py", "old_string": "a", "new_string": "b", "reason": "x"}
    )
    assert "未出现" in edit.invoke(
        {"path": "a.py", "old_string": "zzz", "new_string": "b", "reason": "x"}
    )
    assert "出现了 2 次" in edit.invoke(
        {"path": "a.py", "old_string": "one", "new_string": "b", "reason": "x"}
    )
    assert "无需修改" in edit.invoke(
        {"path": "a.py", "old_string": "two", "new_string": "two", "reason": "x"}
    )
    assert "已修改" in edit.invoke(
        {
            "path": "a.py",
            "old_string": "one",
            "new_string": "b",
            "reason": "x",
            "replace_all": True,
        }
    )


def test_edit_stale_syntax_and_write_failure() -> None:
    fs = _FakeFs({"/ws/a.py": "x = 1\n"})
    tools = _tools(fs)
    edit = tools["file_edit"]

    fs.cache["/ws/a.py"] = b"old\n"
    assert _STALE_READ_MESSAGE in edit.invoke(
        {"path": "a.py", "old_string": "x", "new_string": "y", "reason": "x"}
    )
    fs.cache.clear()

    assert "语法错误" in edit.invoke(
        {"path": "a.py", "old_string": "x = 1", "new_string": "def bad(:", "reason": "x"}
    )

    snap = _FakeSnapshots()
    snap.raises.add("write_with_snapshot")
    assert "写入失败（已中止）" in _tools(fs, snap)["file_edit"].invoke(
        {"path": "a.py", "old_string": "x", "new_string": "y", "reason": "x"}
    )


# ---------------- edit_many ----------------


def test_edit_many_success_and_failures() -> None:
    fs = _FakeFs({"/ws/a.py": "a\n", "/ws/b.py": "b\n"})
    tools = _tools(fs)
    many = tools["file_edit_many"]

    ok = many.invoke(
        {
            "reason": "批量",
            "edits": [
                {"path": "a.py", "old_string": "a", "new_string": "A"},
                {"path": "b.py", "old_string": "b", "new_string": "B"},
            ],
        }
    )
    assert "批量修改 2 个文件" in ok

    assert "不存在" in many.invoke(
        {
            "reason": "批量",
            "edits": [{"path": "missing.py", "old_string": "a", "new_string": "b"}],
        }
    )
    assert "未出现" in many.invoke(
        {
            "reason": "批量",
            "edits": [{"path": "a.py", "old_string": "zzz", "new_string": "b"}],
        }
    )
    assert "old/new 相同" in many.invoke(
        {
            "reason": "批量",
            "edits": [{"path": "a.py", "old_string": "A", "new_string": "A"}],
        }
    )
    assert "语法错误" in many.invoke(
        {
            "reason": "批量",
            "edits": [{"path": "a.py", "old_string": "A", "new_string": "def bad(:"}],
        }
    )


def test_edit_many_scope_path_and_write_failure() -> None:
    fs = _FakeFs({"/ws/a.py": "a\n"})
    scoped = _tools(fs, write_scope=["app"])["file_edit_many"]
    assert "write_scope" in scoped.invoke(
        {
            "reason": "批量",
            "edits": [{"path": "a.py", "old_string": "a", "new_string": "b"}],
        }
    )

    many = _tools(fs)["file_edit_many"]
    assert "路径被拒绝" in many.invoke(
        {
            "reason": "批量",
            "edits": [{"path": "../x", "old_string": "a", "new_string": "b"}],
        }
    )

    snap = _FakeSnapshots()
    snap.raises.add("write_many_with_snapshots")
    assert "批量写入失败" in _tools(fs, snap)["file_edit_many"].invoke(
        {
            "reason": "批量",
            "edits": [{"path": "a.py", "old_string": "a", "new_string": "b"}],
        }
    )


# ---------------- restore ----------------


def test_restore_branches() -> None:
    fs = _FakeFs()
    snap = _FakeSnapshots()
    tools = _tools(fs, snap)
    restore = tools["file_restore"]

    assert "找不到快照" in restore.invoke(
        {"reason": "回滚", "snapshot_id": "nope", "path": "a.py"}
    )

    snap.list_entries = []
    assert "还没有任何快照" in restore.invoke({"reason": "回滚"})

    snap.list_entries = [snap.entry]
    scoped = _tools(fs, snap, write_scope=["app"])["file_restore"]
    assert "write_scope" in scoped.invoke({"reason": "回滚"})

    snap.restore_ok = False
    assert "回滚失败" in restore.invoke({"reason": "回滚"})

    snap.restore_ok = True
    snap.restore_diff = "--- a\n+++ b\n"
    assert "已回滚" in restore.invoke({"reason": "回滚"})


def test_restore_path_rejected() -> None:
    fs = _FakeFs()
    snap = _FakeSnapshots()
    output = _tools(fs, snap)["file_restore"].invoke(
        {"reason": "回滚", "snapshot_id": "s", "path": "../x"}
    )
    assert "路径被拒绝" in output


# ---------------- move / copy / delete / mkdir ----------------


def test_move_and_copy_branches() -> None:
    fs = _FakeFs({"/ws/a.py": "a\n", "/ws/b.py": "b\n"})
    tools = _tools(fs)

    assert "相同" in tools["file_move"].invoke(
        {"source": "a.py", "destination": "a.py", "reason": "x"}
    )
    assert "目标已存在" in tools["file_copy"].invoke(
        {"source": "a.py", "destination": "b.py", "reason": "x"}
    )
    assert "源文件不存在" in tools["file_move"].invoke(
        {"source": "missing.py", "destination": "c.py", "reason": "x"}
    )

    fs.cache["/ws/a.py"] = b"old\n"
    assert _STALE_READ_MESSAGE in tools["file_move"].invoke(
        {"source": "a.py", "destination": "c.py", "reason": "x"}
    )
    fs.cache.clear()

    assert "已复制" in tools["file_copy"].invoke(
        {"source": "a.py", "destination": "c.py", "reason": "x"}
    )
    assert "已移动" in tools["file_move"].invoke(
        {"source": "a.py", "destination": "d.py", "reason": "x"}
    )


def test_move_write_failure_and_remove_failure() -> None:
    fs = _FakeFs({"/ws/a.py": "a\n"})
    snap = _FakeSnapshots()
    snap.raises.add("move_with_snapshot")
    assert "写入目标失败" in _tools(fs, snap)["file_move"].invoke(
        {"source": "a.py", "destination": "c.py", "reason": "x"}
    )

    snap2 = _FakeSnapshots()
    fs.raises["remove"] = SandboxFsError("boom")
    assert "删除源文件失败" in _tools(fs, snap2)["file_move"].invoke(
        {"source": "a.py", "destination": "c.py", "reason": "x"}
    )


def test_delete_branches() -> None:
    fs = _FakeFs({"/ws/a.py": "a\n"})
    tools = _tools(fs)
    delete = tools["file_delete"]

    assert "不存在" in delete.invoke({"path": "missing.py", "reason": "x"})

    fs.cache["/ws/a.py"] = b"old\n"
    assert _STALE_READ_MESSAGE in delete.invoke({"path": "a.py", "reason": "x"})
    fs.cache.clear()

    assert "已删除" in delete.invoke({"path": "a.py", "reason": "x"})

    fs.files["/ws/a.py"] = "a\n"
    fs.raises["remove"] = SandboxFsError("boom")
    assert "删除失败" in delete.invoke({"path": "a.py", "reason": "x"})


def test_mkdir_and_scope_rejections() -> None:
    fs = _FakeFs()
    tools = _tools(fs)
    assert "已创建目录" in tools["file_mkdir"].invoke({"path": "src", "reason": "x"})

    fs.raises["mkdir"] = SandboxFsError("boom")
    assert "创建目录失败" in tools["file_mkdir"].invoke({"path": "src2", "reason": "x"})

    scoped = _tools(fs, write_scope=["app"])
    assert "write_scope" in scoped["file_mkdir"].invoke({"path": "src3", "reason": "x"})
    assert "write_scope" in scoped["file_delete"].invoke({"path": "a.py", "reason": "x"})
    assert "敏感写保护" in tools["file_mkdir"].invoke({"path": ".env", "reason": "x"})
