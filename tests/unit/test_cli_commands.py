"""CLI 子命令。

命令体此前完全没有覆盖。这里用 CliRunner 真正调一遍，
把输出换成记录型 Console 来断言，不依赖真实终端。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from ubismart.audit import AuditLogger, AuditRecord, read_records
from ubismart.cli import app as cli_app
from ubismart.config import Settings
from ubismart.events import ApprovalRequested, RunFinished, RunStarted
from ubismart.session.steering import SteeringController

runner = CliRunner()


@pytest.fixture(autouse=True)
def output(monkeypatch) -> Console:
    console = Console(record=True, width=200, force_terminal=False, no_color=True)
    monkeypatch.setattr(cli_app, "console", console)
    return console


def _text() -> str:
    # clear=False：默认会清空记录缓冲，同一次输出读两遍就变成空串
    return cli_app.console.export_text(clear=False)


def _scoped(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        **overrides,
    )


def _use_settings(monkeypatch, settings: Settings) -> None:
    monkeypatch.setattr(cli_app, "get_settings", lambda: settings)


def _write_records(settings: Settings, count: int = 3) -> AuditLogger:
    logger = AuditLogger(settings.resolved_audit_dir)
    logger.write(AuditRecord(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        kind="run_start", thread_id="abc123", detail="看看目录",
    ))
    for i in range(count):
        logger.write(AuditRecord(
            ts=datetime.now(UTC).isoformat(timespec="seconds"),
            kind="tool_call", thread_id="abc123", tool="shell_exec",
            level="L0 只读", decision="auto", ok=True, duration_ms=10 + i,
        ))
    logger.write(AuditRecord(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        kind="tool_call", thread_id="abc123", tool="shell_exec",
        level="L3 危险", decision="rejected", ok=False,
    ))
    return logger


# ---------------- 帮助 ----------------

@pytest.mark.parametrize(
    "command",
    ["doctor", "sandbox-init", "chat", "run", "repl", "resume", "steer",
     "snapshots", "undo", "diff", "sessions", "history", "checkpoints", "fork",
     "rewind", "memory", "audit", "tui", "gui", "version", "init", "rules"],
)
def test_every_command_has_help(command: str) -> None:
    result = runner.invoke(cli_app.app, [command, "--help"])
    assert result.exit_code == 0
    assert command in result.output or "Usage" in result.output


def test_root_help_lists_commands() -> None:
    result = runner.invoke(cli_app.app, ["--help"])
    assert result.exit_code == 0
    for command in ("doctor", "run", "tui", "undo", "sessions"):
        assert command in result.output


def test_no_args_shows_help() -> None:
    assert runner.invoke(cli_app.app, []).exit_code != 0


def test_show_completion_outputs_a_script(monkeypatch) -> None:
    # Typer 通过 shellingham 探测宿主 shell；CI/conda/无交互终端下探测失败，
    # 会让 `--show-completion` 以 exit 1 收场。显式固定 shell，测试不依赖宿主环境。
    monkeypatch.setattr("typer.completion._get_shell_name", lambda: "bash")
    result = runner.invoke(cli_app.app, ["--show-completion"])

    assert result.exit_code == 0
    assert "COMPLETE" in result.output or "compdef" in result.output


def test_version_option_and_command() -> None:
    expected = cli_app._package_version()

    by_option = runner.invoke(cli_app.app, ["--version"])
    by_command = runner.invoke(cli_app.app, ["version"])

    assert by_option.exit_code == 0
    assert by_command.exit_code == 0
    assert expected in by_option.output
    assert expected in by_command.output


# ---------------- audit ----------------

def test_audit_lists_records(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(cli_app.app, ["audit", "--limit", "2"])

    assert result.exit_code == 0
    text = _text()
    assert "最近 2 条" in text
    assert "tool_call" in text


def test_audit_marks_rejected_rows(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    _write_records(settings)
    _use_settings(monkeypatch, settings)

    runner.invoke(cli_app.app, ["audit", "--limit", "50"])
    assert "已拒绝" in _text()


def test_audit_with_no_records(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path))
    result = runner.invoke(cli_app.app, ["audit"])

    assert result.exit_code == 0
    assert "没有审计记录" in _text()


def test_audit_filters_by_thread(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    logger = _write_records(settings)
    logger.write(AuditRecord(ts="2026-10-03T10:00:00+00:00", kind="run_start",
                             thread_id="other", detail="别的会话"))
    _use_settings(monkeypatch, settings)

    runner.invoke(cli_app.app, ["audit", "--thread-id", "other"])
    assert "别的会话" not in _text()  # run_start 的 detail 不在表格里，但计数要对
    assert "最近 1 条" in _text()


def test_audit_verify_cli(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(cli_app.app, ["audit", "--verify"])

    assert result.exit_code == 0
    assert "哈希链" in _text()


def test_audit_reads_an_explicit_path(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    logger = _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(cli_app.app, ["audit", "--path", str(logger.path)])
    assert result.exit_code == 0
    assert "tool_call" in _text()


def test_audit_json_output(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    logger = _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(
        cli_app.app,
        ["audit", "--path", str(logger.path), "--json", "--limit", "2"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 2
    assert payload[0]["kind"] == "tool_call"


# ---------------- sessions ----------------

def test_sessions_lists_history(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(cli_app.app, ["sessions"])

    assert result.exit_code == 0
    text = _text()
    assert "abc123" in text
    assert "看看目录" in text
    assert "thread-id" in text  # 提示如何接着聊


def test_sessions_when_empty(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path))
    result = runner.invoke(cli_app.app, ["sessions"])

    assert result.exit_code == 0
    assert "没有历史会话记录" in _text()


def test_sessions_respects_limit(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    logger = AuditLogger(settings.resolved_audit_dir)
    for i in range(5):
        logger.write(AuditRecord(
            ts=f"2026-10-0{i + 1}T10:00:00+00:00", kind="run_start",
            thread_id=f"t{i}", detail=f"会话 {i}",
        ))
    _use_settings(monkeypatch, settings)

    runner.invoke(cli_app.app, ["sessions", "--limit", "2"])
    assert "t4" in _text()
    assert "t0" not in _text()


def test_sessions_json_output(tmp_path, monkeypatch) -> None:
    settings = _scoped(tmp_path)
    _write_records(settings)
    _use_settings(monkeypatch, settings)

    result = runner.invoke(cli_app.app, ["sessions", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["thread_id"] == "abc123"


# ---------------- memory ----------------

def test_memory_history_restore_and_provenance(monkeypatch) -> None:
    calls: list[tuple] = []
    backup_id = "20260101T000000000000"

    class FakeMemory:
        path = "/ws/.agent/memory.md"
        last_unsafe: list[str] = []
        last_replaced: list[str] = []

        def history(self, limit: int = 20, *, scope: str = "project"):
            return [{"backup_id": backup_id, "created_at": "2026-01-01T00:00:00"}]

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            self.memory = FakeMemory()

        def remember(self, text, **kwargs):
            calls.append(("remember", text, kwargs))
            return []

        def forget(self, index, **kwargs):
            calls.append(("forget", index, kwargs))
            return []

        def restore_memory(self, value, **kwargs):
            calls.append(("restore", value, kwargs))
            return []

        def memories(self, **kwargs):
            return []

    monkeypatch.setattr(cli_app, "AgentRuntime", FakeRuntime)
    _use_settings(monkeypatch, _scoped_for_history())

    result = runner.invoke(cli_app.app, ["memory", "--history"])
    assert result.exit_code == 0
    assert backup_id in _text()

    result = runner.invoke(
        cli_app.app,
        [
            "memory",
            "--restore",
            backup_id,
            "--thread-id",
            "t1",
        ],
    )
    assert result.exit_code == 0
    assert calls[-1] == (
        "restore",
        backup_id,
        {"thread_id": "t1", "scope": "project"},
    )

    result = runner.invoke(
        cli_app.app,
        [
            "memory",
            "--add",
            "用 pytest",
            "--reason",
            "约定",
            "--ttl-days",
            "7",
            "--thread-id",
            "t1",
        ],
    )
    assert result.exit_code == 0
    assert calls[-1] == (
        "remember",
        "用 pytest",
        {
            "thread_id": "t1",
            "reason": "约定",
            "ttl_days": 7,
            "scope": "project",
        },
    )


def test_memory_vector_status_and_reindex(monkeypatch) -> None:
    class FakeMemory:
        path = "/ws/.agent/memory.md"

        def status(self, *, scope: str = "project"):
            return {
                "enabled": True,
                "provider": "hash",
                "model": "hash-32",
                "dimension": 32,
                "error": "",
                "scopes": {
                    scope: {
                        "facts": 2,
                        "vectors": 1,
                        "missing": 1,
                        "header_ok": True,
                    }
                },
            }

        def reindex(self, *, scope: str = "project", force: bool = False):
            return {
                scope: {"facts": 2, "indexed": 1, "pruned": 0}
            }

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            self.memory = FakeMemory()

    monkeypatch.setattr(cli_app, "AgentRuntime", FakeRuntime)
    _use_settings(monkeypatch, _scoped_for_history())

    result = runner.invoke(cli_app.app, ["memory", "--vector-status"])
    assert result.exit_code == 0
    assert "启用" in _text()
    assert "vectors=1" in _text()

    result = runner.invoke(cli_app.app, ["memory", "--reindex"])
    assert result.exit_code == 0
    assert "索引 1 条" in _text()


# ---------------- rules ----------------

def test_rules_add_and_remove_preserves_other_env_lines(
    tmp_path, monkeypatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "OTHER=1\nAGENT_ALLOW_RULES=shell_exec:git status*\n",
        encoding="utf-8",
    )
    _use_settings(monkeypatch, _scoped_for_history())

    result = runner.invoke(
        cli_app.app,
        ["rules", "--env-file", str(env), "--add", "run_tests:*"],
    )
    assert result.exit_code == 0
    text = env.read_text(encoding="utf-8")
    assert "OTHER=1" in text
    assert "shell_exec:git status*" in text
    assert "run_tests:*" in text

    result = runner.invoke(
        cli_app.app,
        ["rules", "--env-file", str(env), "--remove", "run_tests:*"],
    )
    assert result.exit_code == 0
    text = env.read_text(encoding="utf-8")
    assert "run_tests:*" not in text
    assert "shell_exec:git status*" in text


def test_rules_rejects_bad_format(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped_for_history())
    result = runner.invoke(
        cli_app.app,
        ["rules", "--env-file", str(tmp_path / ".env"), "--add", "bad"],
    )

    assert result.exit_code == 2
    assert "tool:pattern" in _text()


def test_rules_json_output(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("AGENT_ALLOW_RULES=a:b\n", encoding="utf-8")
    _use_settings(monkeypatch, _scoped_for_history())

    result = runner.invoke(
        cli_app.app, ["rules", "--env-file", str(env), "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {"allow_rules": ["a:b"]}


# ---------------- init ----------------

def test_init_creates_env_from_template(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text(
        "DEEPSEEK_API_KEY=\n", encoding="utf-8"
    )

    result = runner.invoke(cli_app.app, ["init"])

    assert result.exit_code == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "DEEPSEEK_API_KEY=\n"
    )
    assert "agent doctor" in _text()


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("NEW=1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OLD=1\n", encoding="utf-8")

    result = runner.invoke(cli_app.app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OLD=1\n"

    result = runner.invoke(cli_app.app, ["init", "--force"])
    assert result.exit_code == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "NEW=1\n"


# ---------------- repl ----------------

def test_repl_runs_turns_and_closes_runtime(monkeypatch) -> None:
    instances = []

    class FakeRuntime:
        persistent = False
        workspace = "/ws"

        def __init__(self, *args, **kwargs) -> None:
            self.calls = []
            self.closed = False
            instances.append(self)

        async def run(self, prompt, *, thread_id, plan_only=False):
            self.calls.append((prompt, thread_id))
            yield RunStarted(thread_id=thread_id)
            yield RunFinished(thread_id=thread_id, answer="好")

        async def resume(self, thread_id, decisions):
            return
            yield  # pragma: no cover

        async def aclose(self) -> None:
            self.closed = True

    inputs = iter(["任务一", "exit"])
    monkeypatch.setattr(cli_app, "AgentRuntime", FakeRuntime)
    monkeypatch.setattr(
        cli_app.console, "input", lambda *args, **kwargs: next(inputs)
    )
    _use_settings(monkeypatch, _scoped_for_history())

    result = runner.invoke(cli_app.app, ["repl"])

    assert result.exit_code == 0
    assert instances[0].calls[0][0] == "任务一"
    assert instances[0].closed is True


# ---------------- history ----------------

def test_history_prints_messages(monkeypatch) -> None:
    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def history(self, thread_id, *, limit=None, offset=0):
            return [
                SimpleNamespace(role="user", text="你好"),
                SimpleNamespace(role="assistant", text="在"),
            ]

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(cli_app, "AgentRuntime", FakeRuntime)
    _use_settings(monkeypatch, _scoped_for_history())

    result = runner.invoke(
        cli_app.app, ["history", "--thread-id", "t1", "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == [
        {"role": "user", "text": "你好"},
        {"role": "assistant", "text": "在"},
    ]


def _scoped_for_history() -> Settings:
    return Settings(_env_file=None, checkpoint_path=":memory:")


# ---------------- doctor ----------------

@pytest.mark.wsl_env
def test_doctor_reports_environment(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path, langsmith_tracing=False))
    result = runner.invoke(cli_app.app, ["doctor"])

    text = _text()
    assert "环境自检" in text
    assert "Python 版本" in text
    assert "依赖 langgraph" in text
    assert "跨进程文件锁" in text
    assert "WSL 发行版" in text
    # 退出码取决于真实环境（WSL/工作区是否就绪），只要求是 0 或 1
    assert result.exit_code in (0, 1)


@pytest.mark.wsl_env
def test_doctor_reports_missing_api_key(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path, deepseek_api_key=""))
    runner.invoke(cli_app.app, ["doctor"])
    assert "DEEPSEEK_API_KEY" in _text()


@pytest.mark.wsl_env
def test_doctor_warns_on_shadowed_config(tmp_path, monkeypatch) -> None:
    """环境变量被 .env 覆盖这件事必须显式说出来。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LANGSMITH_PROJECT=from_dotenv\n", encoding="utf-8")
    monkeypatch.setenv("LANGSMITH_PROJECT", "from_environment")
    monkeypatch.chdir(tmp_path)
    _use_settings(monkeypatch, _scoped(tmp_path, langsmith_tracing=False))

    runner.invoke(cli_app.app, ["doctor"])

    text = _text()
    assert "WARN" in text
    assert "LANGSMITH_PROJECT" in text


# ---------------- run 的参数校验 ----------------

def test_run_requires_a_prompt() -> None:
    assert runner.invoke(cli_app.app, ["run"]).exit_code != 0


def test_run_rejects_unknown_option() -> None:
    assert runner.invoke(cli_app.app, ["run", "--nope", "hi"]).exit_code != 0


def test_tui_reports_missing_ui_extra(monkeypatch) -> None:
    """没装 ui 依赖时要给明确的安装提示，而不是 ImportError 栈。"""

    def boom(name: str, *args, **kwargs):
        raise ImportError("No module named 'textual'")

    monkeypatch.setattr(cli_app, "get_settings", lambda: Settings(_env_file=None))
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "ubismart.tui":
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = runner.invoke(cli_app.app, ["tui"])

    assert result.exit_code == 2
    assert "ui" in _text()


# ---------------- steering ----------------

def test_steer_command_writes_the_persistent_queue(monkeypatch, tmp_path) -> None:
    settings = _scoped(
        tmp_path, steering_dir=str(tmp_path / "steering")
    )
    _use_settings(monkeypatch, settings)

    result = runner.invoke(
        cli_app.app,
        [
            "steer",
            "--thread-id",
            "t1",
            "add",
            "--text",
            "检查边界",
            "--role",
            "review",
        ],
    )

    assert result.exit_code == 0
    controller = SteeringController(settings.resolved_steering_dir)
    commands = controller.peek("t1")
    assert [item.action for item in commands] == ["add"]
    assert commands[0].text == "检查边界"
    assert commands[0].role == "review"

    # CLI 通道也必须留审计：提交时落账，source 标明来源。
    records = read_records(AuditLogger(settings.resolved_audit_dir).path)
    assert records[-1].kind == "steering"
    assert records[-1].source == "cli"
    assert records[-1].thread_id == "t1"
    assert "add: 检查边界" in records[-1].detail


def test_steer_command_honours_an_explicit_queue_dir(monkeypatch, tmp_path) -> None:
    settings = _scoped(
        tmp_path, steering_dir=str(tmp_path / "default-steering")
    )
    _use_settings(monkeypatch, settings)
    explicit = tmp_path / "shared-steering"

    result = runner.invoke(
        cli_app.app,
        [
            "steer",
            "--thread-id",
            "t1",
            "pause",
            "--steering-dir",
            str(explicit),
        ],
    )

    assert result.exit_code == 0
    assert [item.action for item in SteeringController(explicit).peek("t1")] == [
        "pause"
    ]
    # 默认目录不应被写入
    assert SteeringController(settings.resolved_steering_dir).peek("t1") == []


# ---------------- Web 公网守卫 ----------------

def test_loopback_bind_detection() -> None:
    assert cli_app._is_loopback_bind("127.0.0.1")
    assert cli_app._is_loopback_bind("::1")
    assert cli_app._is_loopback_bind("localhost")
    assert not cli_app._is_loopback_bind("0.0.0.0")
    assert not cli_app._is_loopback_bind("203.0.113.10")


def test_remote_web_without_tokens_is_refused(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path, web_auth_tokens=""))

    result = runner.invoke(
        cli_app.app, ["web", "--host", "0.0.0.0", "--allow-remote"]
    )

    assert result.exit_code == 2
    assert "AUTH_TOKENS" in _text()


def test_behind_proxy_without_tokens_is_refused(tmp_path, monkeypatch) -> None:
    _use_settings(monkeypatch, _scoped(tmp_path, web_auth_tokens=""))

    result = runner.invoke(cli_app.app, ["web", "--behind-proxy"])

    assert result.exit_code == 2
    assert "behind-proxy" in _text()


# ---------------- 审批交互 ----------------

def test_yes_does_not_auto_approve_danger(monkeypatch) -> None:
    """--yes 只跳过 L2：L3 危险命令仍要人工回答。"""
    monkeypatch.setattr(
        type(cli_app.console), "input", lambda self, *args, **kwargs: "n"
    )
    requests = [
        ApprovalRequested(request_id="l2", level="L2 变更性", command="pip install x"),
        ApprovalRequested(request_id="l3", level="L3 危险", command="rm -rf /"),
    ]

    decisions = cli_app._ask_approval(requests, assume_yes=True)

    assert decisions == {"l2": True, "l3": False}


# ---------------- main 入口 ----------------

def test_main_reports_missing_api_key(monkeypatch) -> None:
    """缺 Key 时给一句人话，而不是抛栈。"""
    from ubismart.llm.deepseek import MissingApiKeyError

    def boom() -> None:
        raise MissingApiKeyError("未配置 DEEPSEEK_API_KEY。")

    monkeypatch.setattr(cli_app, "app", boom)
    with pytest.raises(typer.Exit) as excinfo:
        cli_app.main()

    assert excinfo.value.exit_code == 2
    assert "DEEPSEEK_API_KEY" in _text()
