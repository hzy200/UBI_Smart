"""轻量控制检查点：round-trip 与线程隔离。"""

from __future__ import annotations

from ubismart.orchestrator.control_checkpoint import ControlCheckpointStore


def test_control_checkpoint_round_trips(tmp_path) -> None:
    store = ControlCheckpointStore(tmp_path / "cp")
    checkpoint_id = store.save(
        {
            "checkpoint_id": "stage-1-v2-3",
            "thread_id": "t1",
            "stage": 1,
            "plan_version": 2,
            "contract": {"goal": "g"},
            "tasks": [{"id": "a"}],
            "task_status": {"a": "success"},
            "artifact_refs": [{"task_id": "a", "artifact_id": "x"}],
            "budget": {"tokens": 10},
            "early_stop": {"token_budget_exhausted": False},
        }
    )

    loaded = store.load("t1", checkpoint_id)
    assert loaded is not None
    assert loaded["contract"]["goal"] == "g"
    assert loaded["artifact_refs"][0]["artifact_id"] == "x"
    assert [item["checkpoint_id"] for item in store.list("t1")] == [
        checkpoint_id
    ]


def test_control_checkpoints_are_isolated_by_thread(tmp_path) -> None:
    store = ControlCheckpointStore(tmp_path / "cp")
    store.save({"checkpoint_id": "a", "thread_id": "t1"})
    store.save({"checkpoint_id": "b", "thread_id": "t2"})

    assert [item["checkpoint_id"] for item in store.list("t1")] == ["a"]
    assert [item["checkpoint_id"] for item in store.list("t2")] == ["b"]
