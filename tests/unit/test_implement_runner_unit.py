"""implement worker 的单元覆盖：不依赖真实 WSL/git。

真实 worktree/patch 路径由 `tests/integration/test_implement_worker.py`（-m wsl）
守着；这里用假 sandbox / 假工具覆盖规划、预算、取消、验证与结果组装分支。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from coding_agent.config import Settings
from coding_agent.orchestrator import implement as implement_module
from coding_agent.orchestrator.implement import make_implement_runner
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.artifacts import pack


class _FakeSandbox:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: str | None = None) -> ExecResult:
        self.commands.append(command)
        return ExecResult(
            command=command, exit_code=0, stdout="", stderr="", duration_ms=0
        )


class _FakeTool:
    def __init__(
        self,
        name: str = "file_edit",
        result: Any = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.result = result if result is not None else pack("ok", {"ok": True})
        self.error = error

    def invoke(self, args: dict[str, Any], config: Any) -> str:
        if self.error is not None:
            raise self.error
        return self.result


class _LLM:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.tools: list[Any] = []
        self.messages: list[Any] = []

    def bind_tools(self, tools: list[Any]) -> _LLM:
        self.tools = tools
        return self

    async def ainvoke(self, messages: list[Any], config: Any = None) -> Any:
        self.messages = messages
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _Verification:
    def __init__(
        self,
        status: str = "ok",
        summary: str = "ok",
        issues: list[dict[str, str]] | None = None,
    ) -> None:
        self.status = status
        self.summary = summary
        self.issues = issues or []

    def model_dump(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "issues": self.issues,
            "output_tail": "",
        }


def _tool_call(name: str = "file_edit", call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": {"path": "app/x.py"},
                "id": call_id,
            }
        ],
    )


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="sk-test",
        audit_dir="/tmp/audit",
        checkpoint_path=":memory:",
        wsl_workspace="/tmp/ws",
        **overrides,
    )


@pytest.fixture
def env(monkeypatch):
    sandbox = _FakeSandbox()
    state = {
        "changed": ["app/x.py"],
        "verification": [_Verification()],
        "patch": "/ws/.agent/patches/t.patch",
        "peer": False,
        "verification_calls": 0,
        "tool": _FakeTool(),
    }

    monkeypatch.setattr(implement_module, "build_tools", lambda *a, **k: [state["tool"]])
    monkeypatch.setattr(implement_module, "_git_ready", lambda sandbox, root: (True, ""))
    monkeypatch.setattr(
        implement_module,
        "_changed_files",
        lambda sandbox, worktree: list(state["changed"]),
    )
    monkeypatch.setattr(
        implement_module, "_impacted_tests", lambda sandbox, worktree, changed: []
    )
    monkeypatch.setattr(
        implement_module,
        "_make_patch",
        lambda sandbox, worktree, root, task_id: state["patch"],
    )
    monkeypatch.setattr(
        implement_module, "_peer_dependency_failure", lambda *a, **k: state["peer"]
    )

    def fake_verify(*args: Any, **kwargs: Any) -> _Verification:
        state["verification_calls"] += 1
        queue = state["verification"]
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    monkeypatch.setattr(implement_module, "run_verification", fake_verify)
    monkeypatch.setattr(
        implement_module, "retry_async", lambda fn, **kwargs: fn()
    )
    return sandbox, state


def _runner(env, responses: list[Any], **settings_kwargs: Any):
    sandbox, state = env
    state.setdefault("tool", _FakeTool())
    settings = _settings(**settings_kwargs)
    return make_implement_runner(
        llm=_LLM(responses),  # type: ignore[arg-type]
        sandbox=sandbox,  # type: ignore[arg-type]
        root="/ws",
        settings=settings,
    )


def _task(**overrides: Any) -> dict[str, Any]:
    task = {
        "id": "implement-1",
        "role": "implement",
        "goal": "改示例",
        "write_scope": ["app"],
    }
    task.update(overrides)
    return task


def test_missing_write_scope_fails(env) -> None:
    runner = _runner(env, [])
    result = asyncio.run(runner(_task(write_scope=[]), parent_request="x"))
    assert result.status == "failed"
    assert "write_scope" in result.summary


def test_git_not_ready_fails(env, monkeypatch) -> None:
    monkeypatch.setattr(
        implement_module, "_git_ready", lambda sandbox, root: (False, "工作区脏")
    )
    runner = _runner(env, [])
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert result.status == "failed"
    assert result.summary == "工作区脏"


def test_worktree_creation_failure_fails(env, monkeypatch) -> None:
    sandbox, state = env
    state["tool"] = _FakeTool()

    class _Broken(_FakeSandbox):
        def run(self, command: str, *, cwd: str | None = None) -> ExecResult:
            self.commands.append(command)
            ok = not command.startswith("git worktree add")
            return ExecResult(
                command=command,
                exit_code=0 if ok else 1,
                stdout="",
                stderr="" if ok else "cannot create",
                duration_ms=0,
            )

    broken = _Broken()
    settings = _settings()
    runner = make_implement_runner(
        llm=_LLM([]),  # type: ignore[arg-type]
        sandbox=broken,  # type: ignore[arg-type]
        root="/ws",
        settings=settings,
    )
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert result.status == "failed"
    assert "创建 worktree 失败" in result.summary
    # 创建失败也要走清理路径。
    assert any("worktree remove" in cmd for cmd in broken.commands)


def test_success_path_builds_full_result(env) -> None:
    runner = _runner(env, [_tool_call(), AIMessage(content="完成")])
    (sandbox, _) = env
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert result.status == "success"
    assert result.files == ["app/x.py"]
    assert result.patch_path == "/ws/.agent/patches/t.patch"
    assert result.tool_calls and result.tool_calls[0]["name"] == "file_edit"
    assert result.budget_used["tool_calls"] == 1
    assert any("worktree add" in cmd for cmd in sandbox.commands)
    assert any("worktree remove" in cmd for cmd in sandbox.commands)


def test_unknown_tool_and_tool_exception_are_recorded(env) -> None:
    sandbox, state = env
    state["tool"] = _FakeTool(error=RuntimeError("tool blew up"))
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "nope", "args": {}, "id": "c0"},
            {"name": "file_edit", "args": {"path": "app/x.py"}, "id": "c1"},
        ],
    )
    runner = _runner(env, [response, AIMessage(content="done")])
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert [call["ok"] for call in result.tool_calls] == [False, False]
    assert [call["name"] for call in result.tool_calls] == ["nope", "file_edit"]


def test_scope_violation_fails_and_is_reported(env) -> None:
    sandbox, state = env
    state["changed"] = ["outside/x.py"]
    state["tool"] = _FakeTool(
        result=pack("rejected", {"ok": True, "rejected": True, "path": "outside/x.py"})
    )
    runner = _runner(env, [_tool_call(), AIMessage(content="done")])
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert result.status == "failed"
    assert "write_scope 外" in result.error
    assert result.scope_violations == ["outside/x.py"]
    assert any("越界修改" in item for item in result.findings)


def test_no_changes_fails(env) -> None:
    _, state = env
    state["changed"] = []
    runner = _runner(env, [_tool_call(), AIMessage(content="done")])
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert result.status == "failed"
    assert "没有产生任何文件改动" in result.error


def test_empty_patch_fails(env) -> None:
    _, state = env
    state["patch"] = ""
    runner = _runner(env, [_tool_call(), AIMessage(content="done")])
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert result.status == "failed"
    assert "无法生成补丁" in result.error


def test_verification_failure_triggers_a_second_attempt(env) -> None:
    sandbox, state = env
    state["verification"] = [
        _Verification(status="failed", summary="boom"),
        _Verification(status="ok", summary="fixed"),
    ]
    runner = _runner(
        env,
        [_tool_call(), AIMessage(content="done")],
        orchestrator_worker_repairs=1,
    )
    result = asyncio.run(
        runner(_task(max_rounds=1), parent_request="x")
    )
    assert result.status == "success"
    assert state["verification_calls"] == 2


def test_peer_dependency_failure_stops_as_partial(env) -> None:
    _, state = env
    state["peer"] = True
    state["verification"] = [_Verification(status="failed", summary="missing module")]
    runner = _runner(env, [_tool_call(), AIMessage(content="done")])
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert result.status == "partial"
    assert result.verification.get("peer_dependency") is True
    assert "主 agent" in result.verification.get("note", "")


def test_token_budget_exhaustion_sets_error(env) -> None:
    response = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 10,
            "total_tokens": 20,
        },
        tool_calls=[],
    )
    runner = _runner(env, [response], orchestrator_task_tokens=5)
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert "token 预算" in result.error
    assert result.input_tokens == 10


def test_cancel_before_first_round_returns_cancelled(env) -> None:
    sandbox, state = env
    state["tool"] = _FakeTool()
    runner = _runner(env, [])
    result = asyncio.run(
        runner(_task(), parent_request="x", cancel=lambda: True)
    )
    assert result.status == "cancelled"
    assert result.patch_path == ""
    assert result.files == []
    assert any("worktree remove" in cmd for cmd in sandbox.commands)


def test_cancel_after_loop_marks_cancelled(env) -> None:
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    runner = _runner(env, [AIMessage(content="done")])
    result = asyncio.run(
        runner(_task(), parent_request="x", cancel=cancel)
    )
    assert result.status == "cancelled"


def test_llm_exception_becomes_failure(env) -> None:
    _, state = env
    state["changed"] = []
    runner = _runner(env, [RuntimeError("provider down")])
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert result.status == "failed"
    assert "provider down" in result.error


def test_timeout_is_marked(env) -> None:
    _, state = env
    state["changed"] = []

    class _Slow:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, config=None):
            await asyncio.sleep(2)
            return AIMessage(content="late")

    settings = _settings(orchestrator_worker_timeout=1)
    runner = make_implement_runner(
        llm=_Slow(),  # type: ignore[arg-type]
        sandbox=env[0],  # type: ignore[arg-type]
        root="/ws",
        settings=settings,
    )
    result = asyncio.run(runner(_task(), parent_request="x"))
    assert result.timed_out is True
    assert "超时" in result.error
