"""code_intel 工具的分支：未配置/只读/文件错误/LSP 成功与失败。"""

from __future__ import annotations

from types import SimpleNamespace

from coding_agent.config import Settings
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.tools.artifacts import unpack
from coding_agent.tools.code_intel import (
    _render_diagnostics,
    _render_locations,
    build_code_intel_tool,
)


class _FS:
    is_file = True
    content = "def f():\n    pass\n"

    def probe(self, path: str):
        return f"/tmp/ws/{path}", SimpleNamespace(
            exists=True, is_file=self.is_file
        )

    def read_text(self, _path: str, max_bytes: int = 0) -> str:
        if not self.is_file:
            raise SandboxPathError("not a file")
        return self.content


class _Lsp:
    def __init__(self, **_kwargs) -> None:
        self.closed = False
        self.fail = False
        self.diagnostics_rows: list[dict] = []

    def initialize(self) -> None:
        pass

    def alive(self) -> bool:
        return not self.closed

    def did_open(self, path: str, text: str) -> None:
        pass

    def definition(self, path: str, line: int, column: int):
        if self.fail:
            raise RuntimeError("lsp crashed")
        return [(path, line, column)]

    def references(self, path: str, line: int, column: int):
        return [(path, line + 1, column)]

    def diagnostics(self, path: str, *, wait: float):
        if self.fail:
            raise RuntimeError("lsp crashed")
        return self.diagnostics_rows

    def close(self) -> None:
        self.closed = True


def _tool(monkeypatch, *, allow_lsp: bool = True, lsp_command: str = "lsp"):
    import coding_agent.tools.code_intel as module

    fake = _Lsp()
    monkeypatch.setattr(module, "SandboxFs", lambda _sandbox, _root: _FS())
    monkeypatch.setattr(module, "LspClient", lambda *a, **k: fake)
    module_fake = fake

    settings = Settings(
        _env_file=None,
        wsl_workspace="/tmp/ws",
        lsp_command=lsp_command,
        max_output_chars=2000,
    )
    tool = build_code_intel_tool(settings, object(), allow_lsp=allow_lsp)
    return tool, module_fake


def _invoke(tool, **args):
    text, artifact = unpack(tool.invoke(args))
    return text, artifact


def test_disabled_and_unconfigured_are_skipped(monkeypatch) -> None:
    tool, _ = _tool(monkeypatch, allow_lsp=False)
    text, artifact = _invoke(
        tool, action="definition", path="a.py", reason="x"
    )
    assert artifact["skipped"] and "只读会话" in text

    tool, _ = _tool(monkeypatch, lsp_command="")
    text, artifact = _invoke(
        tool, action="definition", path="a.py", reason="x"
    )
    assert artifact["skipped"] and "未配置" in text


def test_file_errors_are_reported(monkeypatch) -> None:
    tool, _ = _tool(monkeypatch)
    original = _FS.is_file
    try:
        _FS.is_file = False
        text, artifact = _invoke(
            tool, action="definition", path="a.py", reason="x"
        )
        assert artifact["ok"] is False and "不是普通文件" in text
    finally:
        _FS.is_file = original

    tool, _ = _tool(monkeypatch)
    import coding_agent.tools.code_intel as module

    class _Broken(_FS):
        def read_text(self, *_args, **_kwargs):
            raise SandboxPathError("denied")

    module.SandboxFs = lambda _sandbox, _root: _Broken()
    text, artifact = _invoke(
        tool, action="definition", path="a.py", reason="x"
    )
    assert artifact["ok"] is False and "无法读取" in text


def test_definition_references_and_diagnostics(monkeypatch) -> None:
    tool, fake = _tool(monkeypatch)
    text, artifact = _invoke(
        tool, action="definition", path="a.py", line=2, column=3, reason="定位"
    )
    assert artifact["ok"] and "/tmp/ws/a.py:2:3" in text

    text, _ = _invoke(
        tool, action="references", path="a.py", line=2, column=3, reason="引用"
    )
    assert "/tmp/ws/a.py:3:3" in text

    fake.diagnostics_rows = [
        {
            "severity": 2,
            "range": {"start": {"line": 4}},
            "source": "pyright",
            "code": "reportX",
            "message": "未使用变量",
        }
    ]
    text, artifact = _invoke(
        tool, action="diagnostics", path="a.py", reason="诊断"
    )
    assert artifact["ok"] and "5: [warning] [pyright/reportX]" in text


def test_lsp_failure_disables_client_and_truncates(monkeypatch) -> None:
    tool, fake = _tool(monkeypatch)
    fake.fail = True
    text, artifact = _invoke(
        tool, action="definition", path="a.py", reason="失败"
    )
    assert artifact["ok"] is False and artifact["skipped"]
    assert "LSP 查询失败" in text
    assert fake.closed is True

    # 后续调用不再重复启动 LSP。
    text, artifact = _invoke(
        tool, action="definition", path="a.py", reason="再次"
    )
    assert artifact["skipped"]

    tool, fake = _tool(monkeypatch)
    fake.diagnostics_rows = []
    text, _ = _invoke(
        tool, action="diagnostics", path="a.py", reason="空诊断"
    )
    assert "没有诊断" in text


def test_render_helpers() -> None:
    assert _render_locations([]) == "未找到位置。"
    assert _render_locations([("a.py", 1, 2)]) == "a.py:1:2"
    assert _render_diagnostics([]) == "没有诊断。"
    rendered = _render_diagnostics(
        [
            {"severity": 1, "message": "错误"},
            {
                "severity": 99,
                "range": {"start": {"line": 0}},
                "source": "srv",
                "message": "未知级别",
            },
        ]
    )
    assert "1: [error] 错误" in rendered
    assert "1: [info] [srv] 未知级别" in rendered
