"""WSL 与 Linux 执行后端必须共同满足的契约。"""

from __future__ import annotations

from ubismart.execution import ExecutionRequest, ExecutionService


def assert_execution_contract(
    service: ExecutionService, workspace: str
) -> None:
    caps = service.capabilities()
    assert caps["service"] == "execution"
    assert caps["workspace"] == workspace
    assert caps["shell"] == "bash"

    written = service.execute(
        ExecutionRequest(
            op_id="contract-write",
            kind="write_file",
            run_id="contract",
            payload={"path": "contract.txt", "content": "hello\n"},
        )
    )
    assert written.status == "ok"
    assert written.data["bytes"] == 6

    read = service.execute(
        ExecutionRequest(
            op_id="contract-read",
            kind="read_file",
            run_id="contract",
            payload={"path": "contract.txt"},
        )
    )
    assert read.status == "ok"
    assert read.data["content"] == "hello\n"

    listed = service.execute(
        ExecutionRequest(
            op_id="contract-list",
            kind="list_dir",
            run_id="contract",
            payload={"path": ""},
        )
    )
    assert listed.status == "ok"
    assert any(
        row["path"] == "contract.txt"
        for row in listed.data["entries"]
    )

    stat = service.execute(
        ExecutionRequest(
            op_id="contract-stat",
            kind="stat",
            run_id="contract",
            payload={"path": "contract.txt"},
        )
    )
    assert stat.status == "ok"
    assert stat.data["exists"] is True and stat.data["is_file"] is True

    ran = service.execute(
        ExecutionRequest(
            op_id="contract-run",
            kind="run",
            run_id="contract",
            payload={"command": "printf contract-ok"},
        )
    )
    assert ran.status == "ok"
    assert ran.stdout == "contract-ok"

    failed = service.execute(
        ExecutionRequest(
            op_id="contract-fail",
            kind="run",
            run_id="contract",
            payload={"command": "printf bad >&2; exit 3"},
        )
    )
    assert failed.status == "failed"
    assert failed.exit_code == 3

    replay = service.execute(
        ExecutionRequest(
            op_id="contract-run",
            kind="run",
            run_id="contract",
            payload={"command": "printf contract-ok"},
        )
    )
    assert replay.replayed is True
    assert replay.seq == ran.seq

    escaped = service.execute(
        ExecutionRequest(
            op_id="contract-escape",
            kind="read_file",
            run_id="contract",
            payload={"path": "../../etc/passwd"},
        )
    )
    assert escaped.status == "error"

    snapshot = service.execute(
        ExecutionRequest(
            op_id="contract-snapshot",
            kind="snapshot",
            run_id="contract",
            payload={"path": "contract.txt"},
        )
    )
    assert snapshot.status == "ok"
    assert snapshot.snapshot_ids
    service.execute(
        ExecutionRequest(
            op_id="contract-overwrite",
            kind="write_file",
            run_id="contract",
            payload={"path": "contract.txt", "content": "changed\n"},
        )
    )
    restored = service.execute(
        ExecutionRequest(
            op_id="contract-restore",
            kind="restore",
            run_id="contract",
            payload={
                "snapshot_id": snapshot.snapshot_ids[0],
                "path": "contract.txt",
            },
        )
    )
    assert restored.status == "ok"
    after = service.execute(
        ExecutionRequest(
            op_id="contract-read-after-restore",
            kind="read_file",
            run_id="contract",
            payload={"path": "contract.txt"},
        )
    )
    assert after.data["content"] == "hello\n"

    seqs = [
        written.seq,
        read.seq,
        listed.seq,
        stat.seq,
        ran.seq,
        failed.seq,
        escaped.seq,
        snapshot.seq,
        restored.seq,
        after.seq,
    ]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
