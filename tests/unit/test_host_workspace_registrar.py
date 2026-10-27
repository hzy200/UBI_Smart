"""A2L 本机工作区注册：路径只留在客户端，返回 handle。"""

from __future__ import annotations

import asyncio

import pytest

from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.protocol import CANCELLED
from coding_agent.host_broker.store import WorkspaceRootStore
from coding_agent.host_broker.workspace_registry import TkWorkspaceRegistrar


def test_register_returns_handle_and_stores_root(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    roots: dict[str, str] = {}
    registrar = TkWorkspaceRegistrar(
        roots, a2l_id="a2l-1", picker=lambda: str(project)
    )
    data = asyncio.run(registrar.register())
    assert data["workspace_id"].startswith("ws_")
    assert data["root_handle"].startswith("h_")
    assert data["a2l_id"] == "a2l-1"
    assert roots[data["workspace_id"]] == str(project)

    again = asyncio.run(registrar.register())
    assert again["workspace_id"] == data["workspace_id"]
    assert again["root_handle"] == data["root_handle"]


def test_registered_root_survives_a2l_restart(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = WorkspaceRootStore(tmp_path / "map.db")
    roots: dict[str, str] = {}
    data = asyncio.run(
        TkWorkspaceRegistrar(
            roots,
            a2l_id="a2l-1",
            picker=lambda: str(project),
            store=store,
        ).register()
    )

    # 新进程从本地映射恢复；A2 不需要重新选目录。
    restarted = store.load()
    assert restarted == {data["workspace_id"]: str(project)}


def test_store_upsert_is_idempotent(tmp_path) -> None:
    store = WorkspaceRootStore(tmp_path / "map.db")
    store.upsert(
        "ws-1",
        r"C:\proj",
        display_name="proj",
        root_handle="h-1",
        a2l_id="a2l-1",
    )
    store.upsert(
        "ws-1",
        r"C:\proj",
        display_name="proj",
        root_handle="h-1",
        a2l_id="a2l-1",
    )
    assert store.load() == {"ws-1": r"C:\proj"}
    store.remove("ws-1")
    assert store.load() == {}


def test_register_cancel_is_explicit(tmp_path) -> None:
    registrar = TkWorkspaceRegistrar(
        {}, a2l_id="a2l-1", picker=lambda: ""
    )
    with pytest.raises(BrokerOpError) as exc:
        asyncio.run(registrar.register())
    assert exc.value.code == CANCELLED


def test_register_rejects_non_directory(tmp_path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")
    registrar = TkWorkspaceRegistrar(
        {}, a2l_id="a2l-1", picker=lambda: str(file_path)
    )
    with pytest.raises(BrokerOpError):
        asyncio.run(registrar.register())
