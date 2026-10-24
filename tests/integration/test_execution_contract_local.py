"""Linux 本地执行后端契约（Linux CI 上运行）。"""

from __future__ import annotations

import os

import pytest
from tests.contract.execution_cases import assert_execution_contract

from coding_agent.config import Settings
from coding_agent.execution import ExecutionService, SandboxExecutionBackend
from coding_agent.sandbox.local_exec import LocalLinuxSandbox

pytestmark = [
    pytest.mark.execution,
    pytest.mark.skipif(os.name == "nt", reason="需要 Linux 本地 bash"),
]


def test_local_linux_execution_contract(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        execution_backend="local",
        wsl_workspace=str(tmp_path),
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
    )
    sandbox = LocalLinuxSandbox(settings)
    service = ExecutionService(
        SandboxExecutionBackend(sandbox, str(tmp_path)),
        str(tmp_path),
    )
    try:
        assert_execution_contract(service, str(tmp_path))
    finally:
        service.close()
