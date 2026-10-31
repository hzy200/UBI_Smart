"""上下文压缩：只分叉，不原地删消息，也不复制 tool_calls。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ubismart.config import Settings
from ubismart.runtime import AgentRuntime


class FakeGraph:
    def __init__(self, values: dict) -> None:
        self.values = values
        self.updated: dict | None = None
        self.target: str = ""

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values)

    async def aupdate_state(self, config, values, as_node=None):
        self.target = config["configurable"]["thread_id"]
        self.updated = values


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        session_brief_chars=1_000,
    )


def test_compact_creates_a_text_only_branch(tmp_path) -> None:
    messages = [
        HumanMessage(content="目标一"),
        AIMessage(content="回答一"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "shell_exec", "args": {"command": "ls"}, "id": "c1"}
            ],
        ),
        ToolMessage(content="输出", tool_call_id="c1"),
        HumanMessage(content="目标二"),
        AIMessage(content="回答二"),
    ]
    graph = FakeGraph(
        {
            "messages": messages,
            "cwd": "/tmp/ws",
            "plan": ["旧计划"],
            "step_idx": 2,
            "run_status": "running",
        }
    )
    runtime = AgentRuntime(_settings(tmp_path))
    runtime._graph = graph
    try:
        target = asyncio.run(
            runtime.compact_thread("t1", keep_recent=2, new_thread_id="t1-new")
        )
    finally:
        asyncio.run(runtime.aclose())

    assert target == "t1-new"
    assert graph.updated is not None
    branch = graph.updated["messages"]
    assert isinstance(branch[0], HumanMessage)
    assert "压缩摘要" in branch[0].content
    assert any(isinstance(m, ToolMessage) for m in messages)
    assert not any(isinstance(m, ToolMessage) for m in branch)
    assert not any(getattr(m, "tool_calls", None) for m in branch)
    assert graph.updated["plan"] == []
    assert graph.updated["step_idx"] == 0
    assert graph.updated["run_status"] == "finished"
    assert graph.updated["tasks"] == []
    assert graph.updated["execution_mode"] == "direct"
    assert graph.updated["steering_applied"] == []
    assert graph.updated["recovery_notice"] == ""
    assert graph.updated["session_brief"] == ""
