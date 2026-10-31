"""冷启动首账号：只此一次，且不把控制面 store 句柄交给 CLI。

TD-032 的实质不是"CLI 不该 import control"，而是"CLI 手里不该有能改任意账号
的句柄"。`control/bootstrap.py` 把能力收窄成"租户下一条账号都没有时才允许
创建"，这里把三个分支钉住。
"""

from __future__ import annotations

import pytest

from ubismart.config import Settings
from ubismart.control import bootstrap
from ubismart.control.accounts import AccountError


class _Store:
    def __init__(self, url: str, *, auto_create: bool = False) -> None:
        self.url = url
        self.auto_create = auto_create
        self.engine = object()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Users:
    def __init__(self, engine: object, *, tenant: str = "default") -> None:
        self.engine = engine
        self.tenant = tenant
        self.calls: list[tuple] = []

    def list(self, tenant: str | None = None) -> list[dict]:
        self.calls.append(("list", tenant))
        return []

    def create(self, username, password, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(("create", username, kwargs.get("tenant")))
        return {"username": username, "role": kwargs.get("role", "member")}


def _settings(**overrides) -> Settings:
    values = {
        "control_db_url": "sqlite:///control.db",
        "web_account_tenant": "acme",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _patch(monkeypatch, *, existing: list[dict]) -> dict:
    made: dict = {}

    class Store(_Store):
        def __init__(self, url, *, auto_create=False):  # noqa: ANN001
            super().__init__(url, auto_create=auto_create)
            made["store"] = self

    class Users(_Users):
        def list(self, tenant=None):  # noqa: ANN001
            super().list(tenant)
            return existing

        def create(self, username, password, **kwargs):  # noqa: ANN001, ANN003
            made["users"] = self
            return super().create(username, password, **kwargs)

    monkeypatch.setattr(bootstrap, "ControlStore", Store)
    monkeypatch.setattr(bootstrap, "UserStore", Users)
    return made


def test_requires_a_control_db_url() -> None:
    with pytest.raises(AccountError) as excinfo:
        bootstrap.create_first_admin(
            _settings(control_db_url=""), "alice", "hunter2hunter2"
        )
    assert "AGENT_CONTROL_DB_URL" in str(excinfo.value)


def test_refuses_once_any_account_exists(monkeypatch) -> None:
    """引导命令不能变成"日常加人"的后门。"""
    made = _patch(monkeypatch, existing=[{"username": "bob"}])

    with pytest.raises(AccountError) as excinfo:
        bootstrap.create_first_admin(
            _settings(), "alice", "hunter2hunter2"
        )

    assert "/api/admin/users" in str(excinfo.value)
    assert made["store"].closed is True  # 失败路径也要关连接


def test_creates_the_first_account(monkeypatch) -> None:
    made = _patch(monkeypatch, existing=[])

    row = bootstrap.create_first_admin(
        _settings(), "alice", "hunter2hunter2", email="a@example.com", role="owner"
    )

    assert row == {"username": "alice", "role": "owner"}
    assert made["store"].closed is True
    assert ("create", "alice", "acme") in made["users"].calls
