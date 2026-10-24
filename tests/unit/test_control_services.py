"""Phase B1：SecretProvider 两档与事件传输适配器。"""

from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.control.secrets import (
    KeyringSecretProvider,
    SystemdCredsSecretProvider,
    build_secret_provider,
)
from coding_agent.control.transport import (
    MemoryEventTransport,
    RedisStreamEventTransport,
)
from coding_agent.secrets_store import SecretStoreUnavailable


def test_keyring_provider_delegates_to_store(monkeypatch) -> None:
    data: dict[str, str] = {}

    class FakeStore:
        def __init__(self, service: str = "coding-agent") -> None:
            self.service = service

        def get(self, name: str) -> str:
            return data.get(f"{self.service}:{name}", "")

        def set(self, name: str, value: str) -> None:
            data[f"{self.service}:{name}"] = value

        def delete(self, name: str) -> None:
            data.pop(f"{self.service}:{name}", None)

        def has(self, name: str) -> bool:
            return bool(self.get(name))

    monkeypatch.setattr(
        "coding_agent.control.secrets.SecretStore", FakeStore
    )
    provider = KeyringSecretProvider("coding-agent:alice")
    provider.set("deepseek", "sk-test")
    assert provider.get("deepseek") == "sk-test"
    assert provider.has("deepseek")
    provider.delete("deepseek")
    assert provider.get("deepseek") == ""


def test_systemd_creds_provider_is_read_only(tmp_path) -> None:
    (tmp_path / "deepseek").write_text("sk-systemd\n", encoding="utf-8")
    provider = SystemdCredsSecretProvider(tmp_path)
    assert provider.get("deepseek") == "sk-systemd"
    assert provider.has("deepseek")
    assert provider.get("missing") == ""
    with pytest.raises(SecretStoreUnavailable):
        provider.set("deepseek", "new")
    with pytest.raises(SecretStoreUnavailable):
        provider.delete("deepseek")


def test_build_secret_provider_selects_tier(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    provider = build_secret_provider(
        Settings(_env_file=None, secret_provider="auto")
    )
    assert isinstance(provider, SystemdCredsSecretProvider)

    provider = build_secret_provider(
        Settings(_env_file=None, secret_provider="keyring")
    )
    assert isinstance(provider, KeyringSecretProvider)

    provider = build_secret_provider(
        Settings(_env_file=None, secret_provider="systemd")
    )
    assert isinstance(provider, SystemdCredsSecretProvider)


def test_memory_transport_replays_by_seq() -> None:
    transport = MemoryEventTransport(maxlen=4)
    for seq in (1, 2, 3):
        transport.publish("t1", seq, f"e{seq}")
    assert transport.replay("t1") == [(1, "e1"), (2, "e2"), (3, "e3")]
    assert transport.replay("t1", after=1) == [(2, "e2"), (3, "e3")]
    assert transport.replay("t1", after=0, limit=1) == [(1, "e1")]
    assert transport.replay("other") == []
    assert transport.latest_seq("t1") == 3
    transport.publish("t1", 4, "e4")
    transport.publish("t1", 5, "e5")
    assert transport.replay("t1")[0] == (2, "e2")
    assert transport.latest_seq("t1") == 5
    transport.close()


class FakeRedis:
    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[str, dict]]] = {}
        self.expires: list[tuple[str, int]] = []

    def xadd(self, key, fields, maxlen=None, approximate=True):
        self.rows.setdefault(key, []).append((str(len(self.rows[key]) + 1), fields))
        return self.rows[key][-1][0]

    def xrange(self, key, min="-", max="+"):
        return list(self.rows.get(key, []))

    def xrevrange(self, key, max="+", min="-", count=1):
        return list(self.rows.get(key, []))[-count:]

    def expire(self, key, ttl):
        self.expires.append((key, ttl))

    def close(self):
        pass


def test_redis_stream_transport_publishes_and_replays() -> None:
    redis = FakeRedis()
    transport = RedisStreamEventTransport(
        client=redis, prefix="test:events", ttl_s=60
    )
    transport.publish("t1", 1, "e1")
    transport.publish("t1", 2, "e2")
    assert transport.replay("t1") == [(1, "e1"), (2, "e2")]
    assert transport.replay("t1", after=1) == [(2, "e2")]
    assert transport.replay("t1", after=0, limit=1) == [(1, "e1")]
    assert transport.latest_seq("t1") == 2
    assert redis.expires
    transport.close()
