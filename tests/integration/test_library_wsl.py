"""资料库在真实 WSL 上的摄取/检索冒烟。"""

from __future__ import annotations

import shlex
import uuid

import pytest

from ubismart.library.store import LibraryStore
from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.wsl_exec import WslSandbox


@pytest.mark.wsl
def test_library_store_roundtrip_on_wsl(
    require_wsl: WslSandbox, tmp_path
) -> None:
    sandbox = require_wsl
    root = f"{sandbox.home()}/.agent-library-test-{uuid.uuid4().hex[:8]}"
    sandbox.ensure_directory(root)
    try:
        store = LibraryStore(SandboxFs(sandbox, root), root)
        data = "# 规范\n\n接口必须使用 HTTPS。\n".encode()
        item = store.ingest("spec.md", data)
        assert item["status"] == "ready"
        assert store.search("HTTPS")
        assert store.list_items()[0]["item_id"] == item["item_id"]
        store.delete(item["item_id"])
        assert store.search("HTTPS") == []
    finally:
        sandbox.run(f"rm -rf -- {shlex.quote(root)}")
