"""Git 工具的参数/路径分支（不需要真实 WSL；真实仓库行为在集成测试）。"""

from __future__ import annotations

from typing import Any

from coding_agent.config import Settings
from coding_agent.tools.artifacts import ShellArtifact, unpack
from coding_agent.tools.git import (
    GIT_ADD,
    GIT_COMMIT,
    GIT_DIFF,
    GIT_LOG,
    GIT_STATUS,
    build_git_tools,
)


class _Result:
    def __init__(
        self,
        *,
        ok: bool = True,
        stdout: str = "",
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> None:
        self.ok = ok
        self.stdout = stdout
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.duration_ms = 12

    def render(self, _limit: int) -> str:
        return self.stdout or "ok"


class _Sandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.staged = ""

    def run(self, command: str, cwd: str) -> _Result:
        self.calls.append((command, cwd))
        if command == "git diff --cached --name-only":
            return _Result(stdout=self.staged)
        return _Result(stdout="done")


def _tools() -> tuple[dict[str, Any], _Sandbox]:
    sandbox = _Sandbox()
    settings = Settings(
        _env_file=None,
        wsl_workspace="/tmp/ws",
        max_output_chars=2000,
    )
    return {
        tool.name: tool for tool in build_git_tools(settings, sandbox)
    }, sandbox


def _invoke(tool, **args) -> tuple[str, ShellArtifact]:
    text, artifact = unpack(tool.invoke(args))
    return text, ShellArtifact.model_validate(artifact)


def test_read_only_commands_and_cwd_escape() -> None:
    tools, sandbox = _tools()
    text, artifact = _invoke(tools[GIT_STATUS], reason="看状态")
    assert artifact.ok and "done" in text
    assert sandbox.calls[-1][0] == "git status --short --branch"

    _invoke(tools[GIT_DIFF], reason="看改动", staged=True)
    assert "--cached" in sandbox.calls[-1][0]
    _invoke(tools[GIT_DIFF], reason="看文件", path="src/a.py")
    assert "-- /tmp/ws/src/a.py" in sandbox.calls[-1][0]

    _invoke(tools[GIT_LOG], reason="看历史", limit=3)
    assert "--max-count=3" in sandbox.calls[-1][0]

    text, artifact = _invoke(
        tools[GIT_STATUS], reason="越界", cwd="/etc"
    )
    assert artifact.rejected and "工作目录非法" in text


def test_invalid_diff_path_is_rejected() -> None:
    tools, _ = _tools()
    text, artifact = _invoke(
        tools[GIT_DIFF], reason="越界", path="/etc/passwd"
    )
    assert artifact.rejected and "路径非法" in text


def test_add_rejects_empty_escape_and_agent_state() -> None:
    tools, _ = _tools()
    text, artifact = _invoke(tools[GIT_ADD], reason="空", paths=[])
    assert artifact.rejected and "paths 不能为空" in text

    text, artifact = _invoke(
        tools[GIT_ADD], reason="越界", paths=["/etc/passwd"]
    )
    assert artifact.rejected and "路径非法" in text

    for target in (".agent/x", "."):
        text, artifact = _invoke(
            tools[GIT_ADD], reason="agent", paths=[target]
        )
        assert artifact.rejected
        assert "工作目录" in text

    _invoke(tools[GIT_ADD], reason="正常", paths=["src/a.py"])


def test_commit_rejects_blank_and_staged_agent_state() -> None:
    tools, sandbox = _tools()
    text, artifact = _invoke(tools[GIT_COMMIT], reason="空", message="  ")
    assert artifact.rejected and "提交信息不能为空" in text

    sandbox.staged = ".agent/audit/2026-10-28.jsonl\n"
    text, artifact = _invoke(
        tools[GIT_COMMIT], reason="agent", message="提交"
    )
    assert artifact.rejected and "暂存区里有 agent" in text

    sandbox.staged = ""
    _invoke(tools[GIT_COMMIT], reason="正常", message="fix: x")
    assert sandbox.calls[-1][0] == "git commit -m 'fix: x'"


def test_add_success_and_sandbox_failure_are_reported() -> None:
    tools, sandbox = _tools()
    _invoke(tools[GIT_ADD], reason="正常", paths=["src/a.py"])
    assert sandbox.calls[-1][0] == "git add -- /tmp/ws/src/a.py"

    class _Broken(_Sandbox):
        def run(self, command: str, cwd: str) -> _Result:
            return _Result(ok=False, stdout="fatal: not a repo", exit_code=128)

    settings = Settings(
        _env_file=None, wsl_workspace="/tmp/ws", max_output_chars=2000
    )
    broken = {
        tool.name: tool for tool in build_git_tools(settings, _Broken())
    }
    text, artifact = _invoke(broken[GIT_STATUS], reason="失败")
    assert artifact.ok is False
    assert artifact.exit_code == 128
    assert "fatal" in text


class _Scripted(_Sandbox):
    """按命令返回固定输出，用来构造"输出里带秘密"的场景。"""

    def __init__(self, stdout: str) -> None:
        super().__init__()
        self.output = stdout

    def run(self, command: str, cwd: str) -> _Result:
        self.calls.append((command, cwd))
        if command == "git diff --cached --name-only":
            return _Result(stdout=self.staged)
        return _Result(stdout=self.output)


def _scripted(output: str) -> tuple[dict[str, Any], _Scripted]:
    sandbox = _Scripted(output)
    settings = Settings(
        _env_file=None, wsl_workspace="/tmp/ws", max_output_chars=2000
    )
    tools = {tool.name: tool for tool in build_git_tools(settings, sandbox)}
    return tools, sandbox


def test_diff_of_a_protected_file_is_rejected_before_running() -> None:
    """显式要求看 `.env` 的 diff，等同于读它 —— 与 file_read 同一口径。"""
    tools, sandbox = _scripted("")
    text, artifact = _invoke(tools[GIT_DIFF], reason="看配置", path=".env")
    assert artifact.rejected
    assert "秘密文件读保护" in text
    assert sandbox.calls == []  # 命令根本没跑


def test_diff_output_naming_a_protected_file_is_not_returned() -> None:
    """被误提交的 `.env` 即使整棵树 diff 也不该把内容送进上下文。"""
    tools, _ = _scripted(
        "diff --git a/.env b/.env\n"
        "index 0000000..1111111 100644\n"
        "--- a/.env\n"
        "+++ b/.env\n"
        "@@ -0,0 +1 @@\n"
        "+DB_PASSWORD=hunter2\n"
    )
    text, artifact = _invoke(tools[GIT_DIFF], reason="看改动")
    assert artifact.rejected
    assert ".env" in text
    assert "hunter2" not in text


def test_output_values_are_redacted() -> None:
    """路径没被命中时也不能放过值：`git log -p` 里的 key 照样要脱敏。"""
    tools, _ = _scripted(
        "diff --git a/config.yml b/config.yml\n"
        "--- a/config.yml\n"
        "+++ b/config.yml\n"
        "@@ -1 +1 @@\n"
        "-api_key: sk-abcdefghijklmnopqrstuvwx\n"
        "+api_key: sk-ZZZZZZZZZZZZZZZZZZZZZZZZ\n"
    )
    text, artifact = _invoke(tools[GIT_LOG], reason="看历史提交")
    assert artifact.ok
    assert "sk-abcdefghijklmnopqrstuvwx" not in text
    assert "sk-ZZZZZZZZZZZZZZZZZZZZZZZZ" not in text
    assert "REDACTED" in text


def test_log_body_lines_are_not_mistaken_for_paths() -> None:
    """`git log` 的提交正文可能以 `+++`/`---` 开头，不能据此拒掉输出。"""
    tools, _ = _scripted(
        "commit abc123\n"
        "Author: someone\n"
        "\n"
        "    +++ 补充说明\n"
        "    --- 也不是路径\n"
    )
    text, artifact = _invoke(tools[GIT_LOG], reason="看历史")
    assert artifact.ok
    assert "补充说明" in text


def test_rename_of_a_protected_file_is_rejected() -> None:
    tools, _ = _scripted("rename from .env\nrename to .env.bak\n")
    text, artifact = _invoke(tools[GIT_LOG], reason="看历史")
    assert artifact.rejected
    assert ".env" in text


def test_quoted_diff_header_paths_are_parsed() -> None:
    """路径含空格时 git 会加引号；不能因此漏掉受保护文件。"""
    tools, _ = _scripted('diff --git "a/config/.env" "b/config/.env"\n')
    text, artifact = _invoke(tools[GIT_DIFF], reason="看改动")
    assert artifact.rejected


def test_ordinary_diff_is_returned_unchanged() -> None:
    """反向对照：普通改动不该被误伤。"""
    tools, _ = _scripted(
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1 +1 @@\n"
        "-print('old')\n"
        "+print('new')\n"
    )
    text, artifact = _invoke(tools[GIT_DIFF], reason="看改动")
    assert artifact.ok
    assert "src/a.py" in text
    assert "print('new')" in text
