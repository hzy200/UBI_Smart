"""RFC-0008：verify 失败后的修复轮准备节点。"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from ubismart.graph.nodes.orchestrator_fix import prepare_orchestrator_fix


def test_prepare_fix_builds_request_and_resets_task_graph() -> None:
    out = prepare_orchestrator_fix(
        {
            "messages": [HumanMessage(content="原始需求")],
            "verification": {
                "status": "failed",
                "summary": "2 个测试失败",
                "failed_tests": ["test_a", "test_b"],
                "issues": [{"location": "app/x.py:1", "message": "boom"}],
            },
            "worker_results": [{"task_id": "old", "merged": True}],
            "orchestrator_fix_rounds_used": 0,
            "merged_paths": ["app/x.py"],
        }
    )

    assert out["orchestrator_fix_mode"] is True
    assert out["orchestrator_fix_eligible"] is False
    assert out["orchestrator_fix_rounds_used"] == 1
    assert out["force_workflow"] == "orchestrated_implement"
    assert out["tasks"] == []
    assert out["task_status"] == {}
    assert out["task_extensions_seen"] == 1
    # worker_results 保留：token 预算与审计要能看到上一轮用量。
    assert "worker_results" not in out
    assert "原始需求" in out["request"]
    assert "2 个测试失败" in out["request"]
    assert "test_a" in out["request"]


def test_prepare_fix_increments_round_counter() -> None:
    out = prepare_orchestrator_fix(
        {
            "messages": [],
            "verification": {"status": "failed", "summary": "x"},
            "orchestrator_fix_rounds_used": 2,
        }
    )

    assert out["orchestrator_fix_rounds_used"] == 3
