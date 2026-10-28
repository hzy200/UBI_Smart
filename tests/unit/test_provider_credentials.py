"""上游 provider Key 的加密存储与作用域隔离。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.config import Settings
from coding_agent.control.credentials import (
    CredentialError,
    CredentialStore,
    ScopedSecretStore,
)
from coding_agent.control.store import ControlStore
from coding_agent.web.services import ProviderService

KEY = b"k" * 32


def _store(tmp_path: Path) -> ControlStore:
    return ControlStore(f"sqlite:///{tmp_path / 'control.db'}")


def test_encrypt_roundtrip_and_last4(tmp_path) -> None:
    store = _store(tmp_path)
    secrets = CredentialStore(store.engine, KEY)
    last4 = secrets.set("default", "alice", "deepseek", "sk-abcdefgh1234")
    assert last4 == "1234"
    assert secrets.has("default", "alice", "deepseek")
    assert secrets.get("default", "alice", "deepseek") == "sk-abcdefgh1234"
    assert secrets.last4("default", "alice", "deepseek") == "1234"
    # 明文不落库
    with store.engine.connect() as conn:
        raw = conn.exec_driver_sql(
            "SELECT ciphertext FROM control_provider_keys"
        ).scalar()
    assert "sk-abcdefgh" not in str(raw)
    secrets.delete("default", "alice", "deepseek")
    assert not secrets.has("default", "alice", "deepseek")
    store.close()


def test_users_cannot_read_each_others_keys(tmp_path) -> None:
    store = _store(tmp_path)
    secrets = CredentialStore(store.engine, KEY)
    secrets.set("default", "alice", "deepseek", "alice-key")
    assert secrets.get("default", "bob", "deepseek") == ""
    store.close()


def test_missing_master_key_refuses_write(tmp_path) -> None:
    store = _store(tmp_path)
    secrets = CredentialStore(store.engine, b"")
    with pytest.raises(CredentialError, match="AGENT_SECRET_MASTER_KEY"):
        secrets.set("default", "alice", "deepseek", "sk-x")
    store.close()


def test_tampered_ciphertext_fails_closed(tmp_path) -> None:
    store = _store(tmp_path)
    secrets = CredentialStore(store.engine, KEY)
    secrets.set("default", "alice", "deepseek", "sk-secret")
    with store.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE control_provider_keys SET ciphertext = ? "
            "WHERE user = 'alice'",
            ("AAAA",),
        )
    with pytest.raises(CredentialError, match="解密失败"):
        secrets.get("default", "alice", "deepseek")
    store.close()


class _Fallback:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str) -> str:
        return self.data.get(name, "")

    def set(self, name: str, value: str) -> None:
        self.data[name] = value

    def delete(self, name: str) -> None:
        self.data.pop(name, None)

    def has(self, name: str) -> bool:
        return bool(self.data.get(name))


class _UserKeyStore:
    def get(self, name: str) -> str:
        return "user-key"

    def has(self, name: str) -> bool:
        return True


def test_user_key_overrides_environment_key() -> None:
    providers = json.dumps([
        {
            "name": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "api_key": "env-key",
        }
    ])
    settings = Settings(
        _env_file=None,
        llm_providers=providers,
        deepseek_model="deepseek-chat",
    )
    service = ProviderService(
        settings,
        base_llm_providers=providers,
        is_multi_user=True,
    )
    resolved = service.resolve(settings, _UserKeyStore(), "deepseek")
    assert resolved is not None
    assert resolved["api_key"] == "user-key"


def test_scoped_store_prefers_database_then_fallback(tmp_path) -> None:
    store = _store(tmp_path)
    credentials = CredentialStore(store.engine, KEY)
    fallback = _Fallback()
    fallback.set("legacy", "from-keyring")
    scoped = ScopedSecretStore(credentials, fallback, "default", "alice")
    assert scoped.get("legacy") == "from-keyring"

    scoped.set("deepseek", "db-key")
    assert scoped.get("deepseek") == "db-key"
    assert credentials.get("default", "alice", "deepseek") == "db-key"

    scoped.delete("deepseek")
    assert scoped.get("deepseek") == ""
    store.close()
