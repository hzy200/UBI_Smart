"""A2L 配置默认值：默认关闭、lowpriv、本地 NTFS、随客户端启停。"""

from __future__ import annotations

from coding_agent.config import Settings


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_host_broker_defaults_are_conservative() -> None:
    settings = _settings()
    assert settings.host_broker_enabled is False
    assert settings.grants_enabled is False
    assert settings.host_broker_security_tier == "lowpriv"
    assert settings.host_broker_autostart is True
    assert settings.host_broker_paused is False
    assert settings.workspace_kind_default == "sandbox"
    assert settings.host_root_policy == "local_ntfs_only"


def test_host_broker_aliases_are_stable() -> None:
    settings = _settings(
        AGENT_HOST_BROKER_ENABLED=True,
        AGENT_HOST_BROKER_URL="wss://control.example/a2l",
        AGENT_HOST_BROKER_SECURITY_TIER="appcontainer",
        AGENT_HOST_BROKER_ACCOUNT="agent-host",
        AGENT_HOST_BROKER_HEARTBEAT_S=5,
        AGENT_HOST_BROKER_PAUSED=True,
        AGENT_HOST_BROKER_STORE="C:/agent/ops.sqlite3",
        AGENT_WORKSPACE_KIND_DEFAULT="host",
        AGENT_GRANTS_ENABLED=True,
        AGENT_HOST_ROOT_POLICY="local_ntfs_only",
        AGENT_HOST_SENSITIVE_ROOTS="Documents,Desktop",
    )
    assert settings.host_broker_enabled is True
    assert settings.host_broker_url == "wss://control.example/a2l"
    assert settings.host_broker_security_tier == "appcontainer"
    assert settings.host_broker_account == "agent-host"
    assert settings.host_broker_heartbeat_s == 5
    assert settings.host_broker_paused is True
    assert settings.host_broker_store == "C:/agent/ops.sqlite3"
    assert settings.workspace_kind_default == "host"
    assert settings.grants_enabled is True
    assert settings.host_sensitive_roots == "Documents,Desktop"
