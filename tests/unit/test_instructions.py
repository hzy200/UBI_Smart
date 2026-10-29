"""分层项目指令：发现顺序、合并与容量上限。

发现走 `SandboxFs.find_files`（不拼 shell），所以这里假的是 fs 而不是
sandbox —— 指令文件的内容会直接进模型上下文，这条路径不该再握着"任意命令"
的能力（TD-034）。
"""

from __future__ import annotations

from coding_agent import instructions
from coding_agent.sandbox.fs import FileStat, SandboxFsError


class _Fs:
    """只实现 instructions 用到的三个接口。"""

    def __init__(
        self, files: dict[str, str], *, discover_error: bool = False
    ) -> None:
        self.files = files
        self.discover_error = discover_error
        self.calls: list[tuple] = []

    def find_files(
        self,
        names,
        *,
        max_depth: int = 4,
        exclude_dirs=(),
        limit: int = 200,
    ) -> list[str]:
        self.calls.append(("find_files", tuple(names), max_depth))
        if self.discover_error:
            raise SandboxFsError("find 不可用")
        found = [
            path
            for path in self.files
            if path.rsplit("/", 1)[-1] in names
        ]
        return sorted(found)[:limit]

    def stat(self, path: str) -> FileStat:
        return FileStat(
            exists=path in self.files,
            is_file=True,
            size=len(self.files.get(path, "")),
            real_path=path,
        )

    def read_text(self, path: str, *, max_bytes: int) -> str:
        if path not in self.files:
            raise SandboxFsError("not found")
        return self.files[path]


def _patch(monkeypatch, fs: _Fs) -> _Fs:
    monkeypatch.setattr(
        instructions, "SandboxFs", lambda _sandbox, _root: fs
    )
    return fs


def test_discover_orders_shallow_before_deep(monkeypatch) -> None:
    fs = _patch(
        monkeypatch,
        _Fs(
            {
                "src/deep/AGENTS.md": "",
                "CLAUDE.md": "",
                "src/AGENTS.md": "",
            }
        ),
    )
    found = instructions.discover_instruction_paths(object(), "/work")
    assert found == ["CLAUDE.md", "src/AGENTS.md", "src/deep/AGENTS.md"]
    assert fs.calls[0][2] == instructions.MAX_DEPTH


def test_discover_ignores_unrelated_files(monkeypatch) -> None:
    _patch(monkeypatch, _Fs({"README.md": "", "docs/guide.md": ""}))
    assert instructions.discover_instruction_paths(object(), "/work") == []


def test_discover_falls_back_to_root_when_scan_fails(monkeypatch) -> None:
    """扫描失败时至少看根目录，不因为 find 不可用就整片丢失。"""
    _patch(
        monkeypatch,
        _Fs({"AGENTS.md": "根规则", "src/AGENTS.md": "子规则"}, discover_error=True),
    )
    found = instructions.discover_instruction_paths(object(), "/work")
    assert found == ["AGENTS.md"]


def test_untrusted_repo_instructions_are_not_loaded(monkeypatch) -> None:
    fs = _patch(monkeypatch, _Fs({"AGENTS.md": "根规则"}))

    text = instructions.load_project_instructions(
        object(), "/work", trusted=False
    )

    assert text == ""
    assert fs.calls == []  # 连发现都不做


def test_load_merges_files_with_headers(monkeypatch) -> None:
    _patch(
        monkeypatch,
        _Fs({"CLAUDE.md": "根目录规则", "src/AGENTS.md": "子目录规则"}),
    )
    text = instructions.load_project_instructions(object(), "/work")
    assert "### CLAUDE.md" in text
    assert "### src/AGENTS.md" in text
    # 更具体的子目录指令排在后面
    assert text.index("根目录规则") < text.index("子目录规则")


def test_no_instruction_files_returns_empty(monkeypatch) -> None:
    _patch(monkeypatch, _Fs({}))
    assert instructions.load_project_instructions(object(), "/work") == ""
