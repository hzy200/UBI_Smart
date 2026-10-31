"""WSL 执行后端契约（Windows 本地 `pytest -m wsl`）。"""

from __future__ import annotations

import shlex
import uuid

import pytest
from tests.contract.execution_cases import assert_execution_contract

from ubismart.execution import ExecutionService, SandboxExecutionBackend

pytestmark = [pytest.mark.wsl, pytest.mark.execution]


def test_wsl_execution_contract(require_wsl) -> None:
    workspace = f"/tmp/coding-agent-contract-{uuid.uuid4().hex[:8]}"
    made = require_wsl.run(f"mkdir -p {shlex.quote(workspace)}")
    assert made.ok, made.render(300)
    service = ExecutionService(
        SandboxExecutionBackend(require_wsl, workspace),
        workspace,
    )
    try:
        assert_execution_contract(service, workspace)
    finally:
        service.close()
        require_wsl.run(f"rm -rf {shlex.quote(workspace)}")
