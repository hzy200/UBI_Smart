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


def test_rotate_master_key_reencrypts_and_bumps_version(tmp_path) -> None:
    store = _store(tmp_path)
    new_key = b"n" * 32
    credentials = CredentialStore(store.engine, KEY, key_version=1)
    credentials.set("default", "alice", "deepseek", "sk-rotate-1234")
    assert credentials.rotate(KEY, new_key, new_version=2) == 1

    assert credentials.get(
        "default", "alice", "deepseek"
    ) == "sk-rotate-1234"
    assert credentials.key_version == 2
    with store.engine.connect() as connection:
        version = connection.exec_driver_sql(
            "SELECT key_version FROM control_provider_keys"
        ).scalar()
    assert version == 2
    with pytest.raises(CredentialError, match="解密失败"):
        CredentialStore(store.engine, KEY).get(
            "default", "alice", "deepseek"
        )
    assert CredentialStore(
        store.engine, new_key
    ).get("default", "alice", "deepseek") == "sk-rotate-1234"
    store.close()


def test_master_key_material_scan(tmp_path) -> None:
    import base64

    from coding_agent.control.credentials import find_master_key_material

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    dump = backup_dir / "control.sql"
    dump.write_bytes(b"INSERT ... " + base64.b64encode(KEY))
    assert find_master_key_material(backup_dir, KEY)
    dump.unlink()
    key_file = backup_dir / "master.key"
    key_file.write_text(
        base64.b64encode(KEY).decode("ascii"), encoding="utf-8"
    )
    assert str(key_file.resolve()) in find_master_key_material(
        backup_dir, KEY, key_file=str(key_file)
    )
    key_file.unlink()
    assert find_master_key_material(backup_dir, KEY) == []


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


def test_decode_master_key_accepts_hex_and_rejects_bad_input(monkeypatch) -> None:
    import base64

    from coding_agent.control.credentials import _decode_key, load_master_key

    assert _decode_key("") == b""
    assert _decode_key(KEY.hex()) == KEY
    assert _decode_key(base64.b64encode(KEY).decode("ascii")) == KEY
    # 64 字符但不是 32 字节：按 base64 解出 48 字节后拒绝。
    with pytest.raises(CredentialError, match="32 字节"):
        _decode_key("z" * 64)
    with pytest.raises(CredentialError, match="base64"):
        _decode_key("not-base64!")
    with pytest.raises(CredentialError, match="32 字节"):
        _decode_key(base64.b64encode(b"short").decode("ascii"))

    assert load_master_key(KEY.hex()) == KEY
    monkeypatch.setenv("AGENT_SECRET_MASTER_KEY", KEY.hex())
    assert load_master_key() == KEY
    with pytest.raises(CredentialError, match="无法读取主密钥文件"):
        load_master_key(path=str(Path("no-such-dir") / "master.key"))


def test_master_key_file_roundtrip(tmp_path) -> None:
    import base64

    from coding_agent.control.credentials import load_master_key

    key_file = tmp_path / "master.key"
    key_file.write_text(base64.b64encode(KEY).decode("ascii"), encoding="utf-8")
    assert load_master_key(path=str(key_file)) == KEY


def test_missing_key_cannot_read_existing_ciphertext(tmp_path) -> None:
    store = _store(tmp_path)
    CredentialStore(store.engine, KEY).set(
        "default", "alice", "deepseek", "sk-x"
    )
    with pytest.raises(CredentialError, match="AGENT_SECRET_MASTER_KEY"):
        CredentialStore(store.engine, b"").get(
            "default", "alice", "deepseek"
        )
    store.close()


def test_set_rejects_empty_value_and_counts(tmp_path) -> None:
    store = _store(tmp_path)
    credentials = CredentialStore(store.engine, KEY)
    with pytest.raises(CredentialError, match="不能为空"):
        credentials.set("default", "alice", "deepseek", "")
    assert credentials.count() == 0
    credentials.set("default", "alice", "deepseek", "sk-1")
    credentials.set("default", "alice", "openai", "sk-2")
    assert credentials.count() == 2
    assert credentials.providers_for("default", "alice") == ["deepseek", "openai"]
    store.close()


def test_rotate_validates_keys_and_rolling_back_on_decrypt_failure(
    tmp_path,
) -> None:
    from sqlalchemy.orm import Session

    from coding_agent.control.schema import ControlProviderKey

    store = _store(tmp_path)
    credentials = CredentialStore(store.engine, KEY)
    with pytest.raises(CredentialError, match="32 字节"):
        credentials.rotate(b"short", b"n" * 32)
    with pytest.raises(CredentialError, match="32 字节"):
        credentials.rotate(KEY, b"short")

    # 空 ciphertext 的行跳过（不参与解密，但仍在返回计数里）。
    with Session(store.engine) as session, session.begin():
        session.add(
            ControlProviderKey(
                tenant="default",
                user="alice",
                provider="empty",
                ciphertext="",
                nonce="",
            )
        )
    assert credentials.rotate(KEY, b"n" * 32) == 1

    # 旧主密钥解不开现有密文：轮换必须整体中止。
    credentials.set("default", "alice", "real", "sk-real")
    with pytest.raises(CredentialError, match="轮换中止"):
        credentials.rotate(KEY, b"m" * 32)
    store.close()


def test_scoped_store_decrypt_branches(tmp_path) -> None:
    store = _store(tmp_path)
    credentials = CredentialStore(store.engine, KEY)
    credentials.set("default", "alice", "deepseek", "sk-x")
    fallback = _Fallback()
    scoped = ScopedSecretStore(credentials, fallback, "default", "alice")

    # 没有主密钥但存在密文：fail closed，不回退 fallback。
    broken = ScopedSecretStore(
        CredentialStore(store.engine, b""), fallback, "default", "alice"
    )
    with pytest.raises(CredentialError, match="AGENT_SECRET_MASTER_KEY"):
        broken.get("deepseek")

    # 损坏密文同样 fail closed。
    with store.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE control_provider_keys SET ciphertext = 'AAAA' "
            "WHERE provider = 'deepseek'"
        )
    with pytest.raises(CredentialError, match="解密失败"):
        scoped.get("deepseek")
    assert scoped.has("missing") is False
    assert scoped.last4("missing") == ""
    store.close()


def test_master_key_material_edge_cases(tmp_path) -> None:
    from coding_agent.control.credentials import find_master_key_material

    outside = tmp_path / "outside.key"
    outside.write_text("x", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    # key_file 不在扫描根内：不报，也不误报。
    assert find_master_key_material(
        root, KEY, key_file=str(outside)
    ) == []
    # 空 key 直接返回已有 findings，不再扫内容。
    assert find_master_key_material(root, b"") == []
