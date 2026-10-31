"""runtime 附件清单进入提示词的单元级验证。"""

from __future__ import annotations

from pathlib import Path

from ubismart.runtime import AgentRuntime


def test_attachment_inventory_is_rendered_for_the_current_thread(
    tmp_path: Path,
) -> None:
    uploads = tmp_path / ".agent" / "uploads" / "t1"
    uploads.mkdir(parents=True)
    (uploads / "a.pdf").write_bytes(b"%PDF-1.7")
    (uploads / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    other = tmp_path / ".agent" / "uploads" / "t2"
    other.mkdir(parents=True)
    (other / "secret.txt").write_text("x", encoding="utf-8")

    runtime = object.__new__(AgentRuntime)
    runtime.workspace_kind = "host"
    runtime.file_access = None
    runtime._workspace = str(tmp_path)
    runtime._execution = object()

    text = runtime._attachment_inventory("t1")
    assert "本会话上传的文件" in text
    assert "a.pdf" in text and "b.png" in text
    assert "secret.txt" not in text
    assert runtime._attachment_inventory("") == ""
