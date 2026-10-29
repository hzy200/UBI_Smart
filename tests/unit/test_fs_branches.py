"""SandboxFs 的分支覆盖：用假沙箱把失败/回滚路径拉进 fast 子集。

这些用例不启动 WSL，只验证 Python 侧对脚本结果的解释与错误分类；
真实脚本行为仍由 `-m wsl` 的集成测试守着。
"""

from __future__ import annotations

import base64
import json

import pytest

from coding_agent.sandbox.fs import (
    SOURCES_MARKER,
    SandboxFs,
    SandboxFsError,
    SandboxFsTooLarge,
)
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.wsl_exec import ExecResult

pytestmark = pytest.mark.regression


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(command="", exit_code=0, stdout=stdout, stderr="", duration_ms=0)


def _fail(stderr: str, code: int = 1) -> ExecResult:
    return ExecResult(command="", exit_code=code, stdout="", stderr=stderr, duration_ms=0)


class _FakeSandbox:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.scripts: list[str] = []

    def run(self, script: str, *, cwd: str | None = None, timeout: int | None = None):
        self.scripts.append(script)
        return self.handler(script)


def _fs(handler, *, cache=None) -> SandboxFs:
    fs = SandboxFs(_FakeSandbox(handler), "/ws", cache=cache)  # type: ignore[arg-type]
    fs._real_root = "/ws"
    return fs


def _stat(
    size: int = 3,
    *,
    exists: bool = True,
    is_file: bool = True,
    data: str | None = None,
) -> str:
    lines = [
        f"EXISTS={'1' if exists else '0'}",
        f"FILE={'1' if is_file else '0'}",
        f"SIZE={size}",
        "MTIME=1",
        "REAL=/ws/x",
    ]
    if data is not None:
        lines.append(f"DATA={data}")
    return "\n".join(lines) + "\n"


# ---------------- 缓存与读路径 ----------------


def test_cache_helpers_ignore_outside_paths_and_none_cache() -> None:
    fs = SandboxFs(None, "/ws")  # type: ignore[arg-type]
    assert fs.cached_bytes("a") is None
    fs.invalidate("a")

    cache: dict[str, bytes] = {}
    cached = _fs(lambda _s: _ok(), cache=cache)
    cached.invalidate("../etc/passwd")
    assert cached.cached_bytes("../etc/passwd") is None
    cache["/ws/a"] = b"x"
    assert cached.cached_bytes("/ws/a") == b"x"
    cached.invalidate("/ws/a")
    assert "/ws/a" not in cache


def test_read_bytes_returns_cache_and_rejects_oversized_cache() -> None:
    fs = _fs(lambda _s: pytest.fail("不应启动沙箱"), cache={"/ws/a": b"abc"})
    assert fs.read_bytes("/ws/a", max_bytes=10) == b"abc"
    with pytest.raises(SandboxFsTooLarge):
        fs.read_bytes("/ws/a", max_bytes=2)


def test_read_bytes_error_branches() -> None:
    with pytest.raises(SandboxFsError):
        _fs(lambda _s: _fail("boom")).read_bytes("/ws/x", max_bytes=10)

    with pytest.raises(SandboxFsError, match="不存在"):
        _fs(lambda _s: _ok(_stat(exists=False))).read_bytes("/ws/x", max_bytes=10)

    with pytest.raises(SandboxFsError, match="不是普通文件"):
        _fs(lambda _s: _ok(_stat(is_file=False))).read_bytes("/ws/x", max_bytes=10)

    with pytest.raises(SandboxFsError, match="没有取到内容"):
        _fs(lambda _s: _ok(_stat(data=None))).read_bytes("/ws/x", max_bytes=10)


def test_read_bytes_does_not_cache_a_write_recheck() -> None:
    fs = _fs(lambda _s: _ok(_stat(data=base64.b64encode(b"abc").decode())), cache={})
    assert fs.read_bytes("/ws/x", max_bytes=10, use_cache=False) == b"abc"
    assert "/ws/x" not in fs._cache


def test_read_text_rejects_binary_and_invalid_utf8() -> None:
    nul = base64.b64encode(b"a\x00b").decode()
    with pytest.raises(SandboxFsError, match="二进制"):
        _fs(lambda _s: _ok(_stat(data=nul))).read_text("/ws/x", max_bytes=10)

    bad = base64.b64encode(b"\xff").decode()
    with pytest.raises(SandboxFsError, match="非 UTF-8"):
        _fs(lambda _s: _ok(_stat(data=bad))).read_text("/ws/x", max_bytes=10)


def test_read_for_update_branches() -> None:
    missing = _fs(lambda _s: _ok(_stat(exists=False))).read_for_update("/ws/x", max_bytes=10)
    assert missing[2] is None

    with pytest.raises(SandboxFsError, match="不是普通文件"):
        _fs(lambda _s: _ok(_stat(is_file=False))).read_for_update("/ws/x", max_bytes=10)

    with pytest.raises(SandboxFsTooLarge):
        _fs(lambda _s: _ok(_stat(size=99, data=None))).read_for_update("/ws/x", max_bytes=10)

    with pytest.raises(SandboxFsError, match="没有取到内容"):
        _fs(lambda _s: _ok(_stat(data=None))).read_for_update("/ws/x", max_bytes=10)

    with pytest.raises(SandboxFsError, match="二进制"):
        nul = base64.b64encode(b"a\x00").decode()
        _fs(lambda _s: _ok(_stat(data=nul))).read_for_update("/ws/x", max_bytes=10)

    bad = base64.b64encode(b"\xff").decode()
    with pytest.raises(SandboxFsError, match="非 UTF-8"):
        _fs(lambda _s: _ok(_stat(data=bad))).read_for_update("/ws/x", max_bytes=10)

    text = base64.b64encode("你好".encode()).decode()
    lexical, info, value = _fs(lambda _s: _ok(_stat(data=text))).read_for_update(
        "/ws/x", max_bytes=10
    )
    assert (lexical, info.is_file, value) == ("/ws/x", True, "你好")


# ---------------- 写路径 ----------------


def test_write_bytes_classifies_failures() -> None:
    with pytest.raises(SandboxPathError):
        _fs(lambda _s: _fail("ESCAPED=/etc", 9)).write_bytes("/ws/x", b"x")
    with pytest.raises(SandboxFsError, match="其他进程占用"):
        _fs(lambda _s: _fail("Text file busy")).write_bytes("/ws/x", b"x")
    with pytest.raises(SandboxFsError, match="写入失败"):
        _fs(lambda _s: _fail("Permission denied")).write_bytes("/ws/x", b"x")


def test_write_many_short_circuits_and_classifies_failures() -> None:
    fs = _fs(lambda _s: _ok())
    assert fs.write_many({}) == 0

    with pytest.raises(SandboxPathError):
        _fs(lambda _s: _fail("ESCAPED=/etc", 9)).write_many({"/ws/a": "x"})
    with pytest.raises(SandboxFsError, match="其他进程占用"):
        _fs(lambda _s: _fail("device or resource busy")).write_many({"/ws/a": "x"})
    with pytest.raises(SandboxFsError, match="批量写入失败"):
        _fs(lambda _s: _fail("Permission denied")).write_many({"/ws/a": "x"})

    assert _fs(lambda _s: _ok()).write_many({"/ws/a": "abc"}) == 3


# ---------------- 删除 / 目录 ----------------


def test_remove_branches() -> None:
    missing = _fs(lambda _s: _ok(_stat(exists=False)))
    assert missing.remove("/ws/x") is False

    with pytest.raises(SandboxFsError, match="不是普通文件"):
        _fs(lambda _s: _ok(_stat(is_file=False))).remove("/ws/x")

    with pytest.raises(SandboxPathError):
        _fs(lambda _s: _fail("ESCAPED=/etc", 9)).remove("/ws/x", known_file=True)
    with pytest.raises(SandboxFsError, match="删除失败"):
        _fs(lambda _s: _fail("boom")).remove("/ws/x", known_file=True)

    assert _fs(lambda _s: _ok()).remove("/ws/x", known_file=True) is True


def test_mkdir_branches() -> None:
    with pytest.raises(SandboxPathError):
        _fs(lambda _s: _fail("ESCAPED=/etc", 9)).mkdir("/ws/x")
    with pytest.raises(SandboxFsError, match="不是目录"):
        _fs(lambda _s: _fail("NOTDIR=/ws/x", 10)).mkdir("/ws/x")
    with pytest.raises(SandboxFsError, match="创建目录失败"):
        _fs(lambda _s: _fail("boom")).mkdir("/ws/x")
    with pytest.raises(SandboxFsError, match="未返回真实路径"):
        _fs(lambda _s: _ok("")).mkdir("/ws/x")

    cache = {"/ws/a": b"x"}
    assert _fs(lambda _s: _ok("REAL=/ws/x\n"), cache=cache).mkdir("/ws/x") == "/ws/x"
    assert cache == {}


def test_mkdir_exclusive_failure_branches() -> None:
    with pytest.raises(SandboxPathError):
        _fs(lambda _s: _fail("ESCAPED=/etc", 9)).mkdir_exclusive("/ws/x")
    with pytest.raises(SandboxFsError, match="创建目录失败"):
        _fs(lambda _s: _fail("boom")).mkdir_exclusive("/ws/x")


def test_rmdir_branches() -> None:
    with pytest.raises(SandboxPathError):
        _fs(lambda _s: _fail("ESCAPED=/etc", 9)).rmdir("/ws/x")
    with pytest.raises(SandboxFsError, match="不是目录"):
        _fs(lambda _s: _fail("NOTDIR=/ws/x", 10)).rmdir("/ws/x")
    with pytest.raises(SandboxFsError, match="可能非空"):
        _fs(lambda _s: _fail("boom", 12)).rmdir("/ws/x")

    cache = {"/ws/a": b"x"}
    assert _fs(lambda _s: _ok(), cache=cache).rmdir("/ws/x") is True
    assert cache == {}


# ---------------- 列表 / 查找 ----------------


def test_list_dir_branches() -> None:
    with pytest.raises(SandboxPathError):
        _fs(lambda _s: _fail("ESCAPED=/etc", 9)).list_dir("/ws")
    with pytest.raises(SandboxFsError, match="目录不存在"):
        _fs(lambda _s: _fail("MISSING=1", 12)).list_dir("/ws")
    with pytest.raises(SandboxFsError, match="不是目录"):
        _fs(lambda _s: _fail("NOTDIR=/ws/x", 10)).list_dir("/ws/x")
    with pytest.raises(SandboxFsError, match="列目录失败"):
        _fs(lambda _s: _fail("boom")).list_dir("/ws")

    entries = _fs(lambda _s: _ok("d\tsrc\nf\ta.py\nbroken\n")).list_dir("/ws")
    assert entries == [("d", "src"), ("f", "a.py")]


def test_find_files_branches() -> None:
    assert _fs(lambda _s: _ok()).find_files(["../x", "a/b"]) == []
    with pytest.raises(SandboxFsError, match="查找文件失败"):
        _fs(lambda _s: _fail("boom")).find_files(["AGENTS.md"])
    assert _fs(lambda _s: _ok("src/a.py\n")).find_files(["a.py"]) == ["src/a.py"]


def test_collect_sources_branches() -> None:
    assert _fs(lambda _s: _ok()).collect_sources(["txt"]) == {}

    with pytest.raises(SandboxFsError, match="收集源码失败"):
        _fs(lambda _s: _fail("boom")).collect_sources([".py"])

    with pytest.raises(SandboxFsError, match="无法解析"):
        _fs(lambda _s: _ok(SOURCES_MARKER + "{bad")).collect_sources([".py"])

    with pytest.raises(SandboxFsError, match="不是对象"):
        _fs(lambda _s: _ok(SOURCES_MARKER + "[]")).collect_sources([".py"])

    with pytest.raises(SandboxFsError, match="没有返回结果"):
        _fs(lambda _s: _ok("nothing")).collect_sources([".py"])

    payload = json.dumps({"a.py": "x", "b.py": 1})
    got = _fs(lambda _s: _ok(SOURCES_MARKER + payload)).collect_sources([".py"])
    assert got == {"a.py": "x"}


def test_count_files_branches() -> None:
    assert _fs(lambda _s: _ok()).count_files({"bad-name": ["*.py"]}) == {}

    with pytest.raises(SandboxFsError, match="目录不存在"):
        _fs(lambda _s: _fail("MISSING=1", 12)).count_files({"total": []})
    with pytest.raises(SandboxFsError, match="统计文件数失败"):
        _fs(lambda _s: _fail("boom")).count_files({"total": []})
    with pytest.raises(SandboxFsError, match="不是数字"):
        _fs(lambda _s: _ok("total=nope\n")).count_files({"total": []})

    counts = _fs(lambda _s: _ok("total=12\ntests=3\n")).count_files(
        {"total": [], "tests": ["test_*.py"]}
    )
    assert counts == {"total": 12, "tests": 3}
