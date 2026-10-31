"""Phase 0 WSL 冒烟：本地执行 + SSE/审批/恢复/重放 + 快照接口。

在 WSL 的 venv 中运行：

    PYTHONPATH=src /home/<user>/.coding-agent/venv/bin/python \
        scripts/phase0_wsl_smoke.py

不调用真实模型，不访问网络。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from ubismart import web
from ubismart.config import Settings
from ubismart.events import (
    ApprovalRequested,
    AssistantToken,
    PlanCreated,
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from ubismart.runtime import HistoryMessage
from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.local_exec import LocalLinuxSandbox
from ubismart.sandbox.snapshots import RestoreResult, SnapshotEntry


@dataclass
class FakeRuntime:
    async def run(self, prompt: str, *, thread_id: str):
        yield RunStarted(thread_id=thread_id)
        yield PlanCreated(steps=["读文件"], kinds=["explore"])
        yield AssistantToken(node="act", text="开始")
        yield ToolCallStarted(
            call_id="c1", name="file_read", summary="README.md", level="L0 只读"
        )
        yield ToolCallFinished(call_id="c1", name="file_read", ok=True)
        yield ApprovalRequested(
            request_id="approval-1",
            tool="shell_exec",
            command="grep TODO .",
            level="L2 变更性",
        )

    async def resume(self, thread_id: str, decisions: dict[str, bool]):
        yield RunFinished(thread_id=thread_id, answer="done")

    async def pending_approvals(self, thread_id: str):
        return [
            ApprovalRequested(
                request_id="approval-1",
                tool="shell_exec",
                command="grep TODO .",
                level="L2 变更性",
            )
        ]

    def steer(self, thread_id: str, action: str, **kwargs) -> None:
        return

    def is_paused(self, thread_id: str) -> bool:
        return False

    async def history(self, thread_id: str, *, limit=None, offset=0):
        return [HistoryMessage("user", "hi")]

    def list_snapshots(self, *, limit: int = 20):
        return [SnapshotEntry("20260101T000000-a1", "README.md")]

    def diff_snapshot(self, *, path=None, snapshot_id=None) -> str:
        return "--- a/README.md\n+++ b/README.md\n"

    def restore(self, *, path=None, snapshot_id=None):
        return RestoreResult(ok=True, path=path or "", snapshot_id=snapshot_id or "")

    def restore_since(self, prefix: str, *, dry_run: bool = False):
        return [RestoreResult(ok=True, path="README.md")]

    def last_run_started_at(self, thread_id: str | None = None) -> str:
        return "20260101T000000"

    async def checkpoints(self, thread_id: str, *, limit: int = 20):
        return [{"checkpoint_id": "cp1", "step": 0, "preview": "hi"}]

    async def fork(self, thread_id: str, *, checkpoint_id="", new_thread_id=""):
        return f"{thread_id}-fork"

    async def rewind(self, thread_id: str, *, checkpoint_id="", steps=1):
        return "cp0"

    async def compact_thread(self, thread_id: str, *, keep_recent=6, new_thread_id=""):
        return f"{thread_id}-compact"

    def memories(self, *, scope=None):
        return ["pytest"]

    def remember(self, text: str, *, thread_id="", scope="project"):
        return [text]

    def forget(self, index: int, *, thread_id="", scope="project"):
        return []

    def clear_memory(self, *, thread_id="", scope="project"):
        return []

    def pin_memory(self, index: int, *, pinned: bool, thread_id="", scope="project"):
        return []

    def set_memory_priority(self, index: int, priority: int, *, thread_id="", scope="project"):
        return []

    async def aclose(self) -> None:
        return


def frames(text: str) -> list[tuple[int | None, dict]]:
    out: list[tuple[int | None, dict]] = []
    for frame in text.split("\n\n"):
        seq = None
        data = []
        for line in frame.splitlines():
            if line.startswith("id:"):
                seq = int(line[3:].strip())
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            out.append((seq, json.loads("\n".join(data))))
    return out


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="phase0-"))
    workspace = root / "ws"
    workspace.mkdir()
    settings = Settings(
        _env_file=None,
        audit_dir=str(root / "audit"),
        checkpoint_path=str(root / "cp.sqlite"),
        deepseek_api_key="sk-test",
        wsl_workspace=str(workspace),
        execution_backend="local",
        shell_sandbox="off",
    )

    sandbox = LocalLinuxSandbox(settings)
    result = sandbox.run("printf phase0; pwd", cwd=str(workspace))
    assert result.ok and "phase0" in result.stdout, result.render()
    fs = SandboxFs(sandbox, str(workspace))
    assert fs.write_text("smoke.txt", "ok\n") == 3
    assert fs.read_text("smoke.txt", max_bytes=100).strip() == "ok"

    from ubismart.sandbox.snapshots import SnapshotStore

    snapshots = SnapshotStore(sandbox, fs, str(workspace))
    snapshots.save("smoke.txt", "ok\n")
    fs.write_text("smoke.txt", "changed\n")
    entry = snapshots.latest_for("smoke.txt")
    assert entry is not None
    restored = snapshots.restore(entry)
    assert restored.ok, restored.message
    assert fs.read_text("smoke.txt", max_bytes=100).strip() == "ok"
    print("PASS local sandbox + SandboxFs + snapshot/restore")

    web.app.AgentRuntime = lambda *a, **k: FakeRuntime()  # type: ignore[assignment]
    client = TestClient(
        web.create_app(
            settings,
            allow_write=True,
            default_access_mode="write_ask",
            stream_buffer=True,
        )
    )
    created = client.post(
        "/api/sessions",
        json={"workspace": str(workspace), "access_mode": "write_ask"},
    ).json()
    thread = created["thread_id"]

    run = client.post("/api/run", json={"prompt": "hi", "thread_id": thread})
    run_frames = frames(run.text)
    assert run_frames[-1][1]["type"] == "approval_requested"
    last_seq = run_frames[-1][0]
    assert client.get(f"/api/approvals/{thread}").json()["approvals"]
    print("PASS SSE approval suspension")

    resumed = client.post(f"/api/resume/{thread}", json={"decisions": {"approval-1": True}})
    resumed_frames = frames(resumed.text)
    assert resumed_frames[-1][1]["type"] == "run_finished"
    assert resumed_frames[0][0] == last_seq + 1
    print("PASS resume + monotonic event id")

    replay = frames(client.get(f"/api/stream/{thread}?after={last_seq}").text)
    assert replay and replay[-1][1]["type"] == "run_finished"
    assert client.get(f"/api/snapshots?thread_id={thread}").json()["snapshots"]
    assert client.get(f"/api/diff?thread_id={thread}&path=README.md").json()["diff"]
    assert client.post(
        "/api/restore",
        json={"thread_id": thread, "path": "README.md"},
    ).json()["ok"]
    assert client.post(
        f"/api/context/{thread}/compact", json={"keep_recent": 2}
    ).json()["thread_id"].endswith("-compact")
    print("PASS replay + snapshot/restore + compact")
    print("PHASE0 SMOKE OK", root)


if __name__ == "__main__":
    main()
