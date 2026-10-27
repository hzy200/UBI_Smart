"""FileAccess 协议：只读三工具不直接依赖 WSL 路径。"""

from __future__ import annotations

from types import SimpleNamespace

from coding_agent.execution.file_access import FileAccess, SandboxFileAccess


class FakeFs:
    root = "/ws"

    def __init__(self) -> None:
        self.tree = {
            "/ws": [("d", "src"), ("f", "README.md")],
            "/ws/src": [("f", "main.py"), ("d", "pkg"), ("l", "link")],
            "/ws/src/pkg": [("f", "util.py")],
        }

    def lexical(self, path: str) -> str:
        if path in ("", "."):
            return "/ws"
        if path.startswith("/"):
            return path
        return f"/ws/{path}"

    def stat(self, path: str):
        return SimpleNamespace(exists=True, size=1, real_path=path)

    def read_text(self, path: str, *, max_bytes: int) -> str:
        return f"{self.lexical(path)}:{max_bytes}"

    def list_dir(self, path: str, *, max_depth: int = 1, limit: int = 200):
        return list(self.tree.get(self.lexical(path), []))[:limit]


def test_sandbox_adapter_matches_protocol() -> None:
    access = SandboxFileAccess(FakeFs(), "/ws")
    assert isinstance(access, FileAccess)
    assert access.lexical("src/main.py") == "/ws/src/main.py"
    assert access.read_text("src/main.py", max_bytes=10) == "/ws/src/main.py:10"
    assert access.list_dir("src") == [
        ("f", "main.py"),
        ("d", "pkg"),
        ("l", "link"),
    ]


def test_find_files_does_not_follow_symlinks_and_respects_limit() -> None:
    access = SandboxFileAccess(FakeFs(), "/ws")
    assert access.find_files("*.py") == ["src/main.py", "src/pkg/util.py"]
    assert access.find_files("*.py", limit=1) == ["src/main.py"]
    assert access.find_files("README*") == ["README.md"]
    assert access.find_files("") == []
