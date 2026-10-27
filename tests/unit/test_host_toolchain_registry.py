"""工具链能力注册：只暴露摘要，不暴露宿主绝对路径。"""

from __future__ import annotations

import json

import pytest

from coding_agent.host_broker.toolchain import ToolchainRef, ToolchainRegistry


def test_summary_has_no_absolute_path() -> None:
    registry = ToolchainRegistry(
        [
            ToolchainRef(
                id="msvc-14.41",
                kind="cpp",
                version="14.41",
                output_roots=("build", "out/x64"),
            )
        ]
    )
    payload = registry.summary()
    assert payload["toolchains"][0]["id"] == "msvc-14.41"
    assert payload["toolchains"][0]["output_roots"] == ["build", "out/x64"]
    serialized = json.dumps(payload)
    assert "C:\\" not in serialized
    assert "/usr/" not in serialized


def test_register_duplicate_and_invalid_ids() -> None:
    registry = ToolchainRegistry()
    ref = ToolchainRef(id="python-3.11", kind="python")
    registry.register(ref)
    with pytest.raises(ValueError):
        registry.register(ref)
    for bad in ("../x", "a/b", "", "a b"):
        with pytest.raises(ValueError):
            ToolchainRef(id=bad, kind="python")
    with pytest.raises(ValueError):
        ToolchainRef(id="x", kind="Python!")


def test_output_roots_must_be_relative_and_safe() -> None:
    for bad in ("/abs", "C:\\build", "../out", "a/../../out", ""):
        with pytest.raises(ValueError):
            ToolchainRef(id="x", kind="cpp", output_roots=(bad,))


def test_get_iter_and_clear() -> None:
    ref = ToolchainRef(id="node-20", kind="node", version="20")
    registry = ToolchainRegistry([ref])
    assert registry.get("node-20") == ref
    assert list(registry) == [ref]
    assert len(registry) == 1
    registry.clear()
    assert len(registry) == 0
