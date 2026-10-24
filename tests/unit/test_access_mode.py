"""会话访问模式在 runtime 入口的强制效果。"""

from __future__ import annotations

import asyncio

from coding_agent.config import Settings
from coding_agent.runtime import AgentRuntime


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
    )


def test_read_only_disables_verify_and_orchestrator(tmp_path) -> None:
    runtime = AgentRuntime(_settings(tmp_path), access_mode="read_only")
    try:
        assert runtime.allow_write is False
        assert runtime.settings.verify_enabled is False
        assert runtime.settings.orchestrator_enabled is False
        assert runtime.policy.access_mode == "read_only"
    finally:
        asyncio.run(runtime.aclose())


def test_write_ask_exposes_tools_but_policy_asks(tmp_path) -> None:
    runtime = AgentRuntime(_settings(tmp_path), access_mode="write_ask")
    try:
        assert runtime.allow_write is True
        assert runtime.settings.verify_enabled is True
        assert runtime.policy.access_mode == "write_ask"
    finally:
        asyncio.run(runtime.aclose())
