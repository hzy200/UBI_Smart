"""Phase 1 只读能力：grant/ZX 之后才执行。"""

from __future__ import annotations

import asyncio
import os

import pytest

from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.ops import HostReadOnlyOps
from coding_agent.host_broker.paths import HostPathPolicy
from coding_agent.host_broker.policy import GrantStore, HostPolicy
from coding_agent.host_broker.protocol import (
    DENIED_ZX,
    GRANT_REQUIRED,
    OP_NOT_ENABLED_IN_PHASE1,
    make_request,
)
from coding_agent.host_broker.toolchain import ToolchainRef, ToolchainRegistry


def _ops(tmp_path, *, deny=(), sensitive=()):
    root = tmp_path / "project"
    root.mkdir()
    policy = HostPolicy(
        deny_roots=[str(item) for item in deny],
        sensitive_roots=[str(item) for item in sensitive],
    )
    grants = GrantStore()
    toolchains = ToolchainRegistry(
        [ToolchainRef(id="python-3.11", kind="python", version="3.11")]
    )
    ops = HostReadOnlyOps(
        policy=policy,
        path_policy=HostPathPolicy(),
        grants=grants,
        toolchains=toolchains,
        workspace_roots={"ws-1": str(root)},
        broker_id="b-1",
    )
    return ops, grants, root


def _frame(kind, grant_id="", payload=None):
    return make_request(
        kind,
        op_id="op-1",
        workspace={"workspace_id": "ws-1", "kind": "host", "root_handle": "h-1"},
        payload=payload or {},
        grant_id=grant_id,
    )


def test_read_requires_grant_then_succeeds(tmp_path) -> None:
    ops, grants, root = _ops(tmp_path)
    (root / "a.py").write_text("print('x')\n", encoding="utf-8")

    with pytest.raises(BrokerOpError) as exc:
        asyncio.run(ops.handle(_frame("host.read_text", payload={"path": "a.py"})))
    assert exc.value.code == GRANT_REQUIRED

    grant = grants.issue(scope=[str(root)], verbs=["read"])
    response = asyncio.run(
        ops.handle(
            _frame(
                "host.read_text",
                grant_id=grant.grant_id,
                payload={"path": "a.py"},
            )
        )
    )
    assert response.status == "ok"
    assert "print" in response.data["text"]


def test_unknown_write_exec_and_install_are_manual_or_denied(
    tmp_path,
) -> None:
    ops, grants, root = _ops(tmp_path)
    (root / "a.py").write_text("x", encoding="utf-8")
    grant = grants.issue(
        scope=[str(root)], verbs=["read", "write", "exec", "install"]
    )
    # 未实现的写 op 仍拒绝。
    with pytest.raises(BrokerOpError) as exc:
        asyncio.run(
            ops.handle(
                _frame(
                    "host.write_bytes",
                    grant_id=grant.grant_id,
                    payload={"path": "a.py"},
                )
            )
        )
    assert exc.value.code == OP_NOT_ENABLED_IN_PHASE1

    # 未开启 exec 时 fail closed。
    with pytest.raises(BrokerOpError) as exc:
        asyncio.run(
            ops.handle(
                _frame(
                    "host.exec",
                    grant_id=grant.grant_id,
                    payload={"path": "a.py"},
                )
            )
        )
    assert exc.value.code == "EXEC_UNAVAILABLE"

    # install 永远只返回人工指令，不执行。
    install = asyncio.run(
        ops.handle(
            _frame(
                "host.install",
                grant_id=grant.grant_id,
                payload={
                    "manager": "pip",
                    "command": "pip install requests",
                },
            )
        )
    )
    assert install.data["manual"] is True
    assert install.data["executed"] is False

    for rel in ("../outside.txt",):
        with pytest.raises(BrokerOpError) as exc:
            asyncio.run(
                ops.handle(
                    _frame(
                        "host.read_text",
                        grant_id=grant.grant_id,
                        payload={"path": rel},
                    )
                )
            )
        assert exc.value.code in {"PATH_ESCAPE", "INVALID_PATH"}

    if os.name == "nt":
        for rel in (r"\\server\share\a.txt", "a.txt:secret"):
            with pytest.raises(BrokerOpError) as exc:
                asyncio.run(
                    ops.handle(
                        _frame(
                            "host.read_text",
                            grant_id=grant.grant_id,
                            payload={"path": rel},
                        )
                    )
                )
            assert exc.value.code in {"PATH_ESCAPE", "INVALID_PATH"}


def test_zx_wins_over_grant(tmp_path) -> None:
    ops, grants, root = _ops(tmp_path, deny=[tmp_path / "project"])
    grant = grants.issue(scope=[str(root)], verbs=["read"])
    with pytest.raises(BrokerOpError) as exc:
        asyncio.run(
            ops.handle(
                _frame(
                    "host.read_text",
                    grant_id=grant.grant_id,
                    payload={"path": "a.py"},
                )
            )
        )
    assert exc.value.code == DENIED_ZX


def test_list_stat_toolchains_and_env(tmp_path, monkeypatch) -> None:
    ops, grants, root = _ops(tmp_path)
    (root / "a.py").write_text("x", encoding="utf-8")
    (root / "sub").mkdir()
    grant = grants.issue(scope=[str(root)], verbs=["read"])

    listed = asyncio.run(
        ops.handle(
            _frame("host.list_dir", grant_id=grant.grant_id, payload={"path": "."})
        )
    )
    names = {item["path"] for item in listed.data["entries"]}
    assert "a.py" in names and "sub" in names

    stat = asyncio.run(
        ops.handle(
            _frame("host.stat", grant_id=grant.grant_id, payload={"path": "a.py"})
        )
    )
    assert stat.data["exists"] is True
    assert stat.data["is_file"] is True

    toolchains = asyncio.run(ops.handle(_frame("host.toolchains")))
    assert toolchains.data["toolchains"][0]["id"] == "python-3.11"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    monkeypatch.setenv("HOME", "/home/test")
    env = asyncio.run(
        ops.handle(
            _frame(
                "host.env.inspect",
                grant_id=grant.grant_id,
                payload={"names": ["DEEPSEEK_API_KEY", "HOME"]},
            )
        )
    )
    assert "DEEPSEEK_API_KEY" not in env.data["values"]
    assert env.data["values"]["HOME"] == "/home/test"


class FakeRegistrar:
    async def register(self):
        return {
            "workspace_id": "ws-new",
            "root_handle": "h-new",
            "display_name": "new",
            "a2l_id": "b-1",
        }


def test_workspace_register_uses_local_registrar(tmp_path) -> None:
    ops, grants, root = _ops(tmp_path)
    ops.workspace_registrar = FakeRegistrar()
    response = asyncio.run(
        ops.handle(_frame("host.workspace.register"))
    )
    assert response.data["workspace_id"] == "ws-new"
    assert response.data["root_handle"] == "h-new"


def test_unknown_workspace_and_op(tmp_path) -> None:
    ops, _, _ = _ops(tmp_path)
    frame = make_request(
        "host.read_text",
        op_id="op-1",
        workspace={"workspace_id": "missing", "kind": "host", "root_handle": "h"},
        payload={"path": "a.py"},
    )
    with pytest.raises(BrokerOpError):
        asyncio.run(ops.handle(frame))
    with pytest.raises(BrokerOpError):
        asyncio.run(ops.handle(_frame("host.write_bytes")))
