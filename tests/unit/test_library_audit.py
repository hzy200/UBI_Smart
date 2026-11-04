"""资料库审计 kind 与审计参数脱敏。"""

from __future__ import annotations

import json

from ubismart.audit import models as audit_models
from ubismart.audit.logger import sanitize_args
from ubismart.runtime.library import LibraryMixin


class _AuditRuntime(LibraryMixin):
    workspace = "/ws"

    def __init__(self) -> None:
        self.records = []

    def _audit(self, record) -> None:  # noqa: ANN001 - 测试桩
        self.records.append(record)


def test_library_audit_records_kind_action_and_item() -> None:
    runtime = _AuditRuntime()
    runtime._audit_library(
        "ingest", "lib_abc", ok=True, detail="ready", added=12
    )
    record = runtime.records[0]
    assert record.kind == audit_models.LIBRARY
    assert record.action == "ingest"
    assert record.path == "lib_abc"
    assert record.ok is True
    assert record.added == 12
    assert record.workspace == "/ws"


def test_audit_args_redact_library_query_secrets() -> None:
    args = sanitize_args(
        {"query": "api key sk-abcdefghijklmnopqrstuvwxyz012345"}
    )
    payload = json.dumps(args, ensure_ascii=False)
    assert "sk-" not in payload
    assert "query" in args
