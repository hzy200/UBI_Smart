"""Phase 4：只读文件树与 Git 面板。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from coding_agent.workspace.browse import (
    BrowseError,
    git_summary,
    list_directory,
    read_text_file,
    search_workspace,
)


def test_list_directory_is_relative_and_hides_noise(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")

    data = list_directory(str(tmp_path))
    assert data["path"] == ""
    assert [row["name"] for row in data["entries"]] == [
        "src",
        "README.md",
    ]
    assert data["entries"][0]["is_dir"] is True

    nested = list_directory(str(tmp_path), "src")
    assert nested["entries"][0]["path"] == "src/a.py"


def test_read_file_reports_binary_and_truncation(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("你好", encoding="utf-8")
    data = read_text_file(str(tmp_path), "a.txt")
    assert data["content"] == "你好"
    assert data["binary"] is False

    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    assert read_text_file(str(tmp_path), "b.bin")["binary"] is True

    (tmp_path / "huge.txt").write_text("x" * (300 * 1024), encoding="utf-8")
    big = read_text_file(str(tmp_path), "huge.txt")
    assert big["truncated"] is True
    assert len(big["content"]) < 300 * 1024


def test_search_matches_paths_and_content_without_secrets(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "def needle():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "needle.md").write_text("hello\n", encoding="utf-8")
    (tmp_path / ".env").write_text("NEEDLE=secret\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "needle.js").write_text(
        "needle\n", encoding="utf-8"
    )

    data = search_workspace(str(tmp_path), "needle")
    paths = {(row["path"], row["kind"]) for row in data["results"]}
    assert ("docs/needle.md", "path") in paths
    assert ("src/main.py", "content") in paths
    assert all(row["path"] != ".env" for row in data["results"])
    assert all("node_modules" not in row["path"] for row in data["results"])


def test_search_is_bounded(tmp_path) -> None:
    for index in range(10):
        (tmp_path / f"hit-{index}.txt").write_text("hit\n", encoding="utf-8")
    data = search_workspace(str(tmp_path), "hit", limit=3)
    assert len(data["results"]) == 3
    assert data["truncated"] is True


def test_paths_cannot_escape_workspace(tmp_path) -> None:
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(BrowseError):
            list_directory(str(tmp_path), bad)


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_git_summary_reads_status_and_log(tmp_path) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
        )

    git("init")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-m", "first")
    (tmp_path / "a.txt").write_text("two\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    data = git_summary(str(tmp_path), diff=True)
    assert data["is_repo"] is True
    assert data["commits"][0]["subject"] == "first"
    assert {row["path"] for row in data["changes"]} == {"a.txt", "new.txt"}
    assert "a.txt" in data["diff"]


def test_non_repo_is_reported(tmp_path) -> None:
    data = git_summary(str(tmp_path))
    assert data["is_repo"] is False
