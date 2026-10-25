"""RFC-0001：工作区配置项的默认值与策略校验。"""

from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.control.workspaces import (
    WorkspaceError,
    WorkspacePathError,
    WorkspacePolicy,
)


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_workspace_defaults_are_conservative() -> None:
    settings = _settings()
    assert settings.workspace_registry is False
    assert settings.workspace_root == ""
    assert settings.workspace_archive_root == ""
    assert settings.workspace_revisions_keep == 10
    assert settings.workspace_import_mode == "copy"
    assert settings.workspace_scan_provider == "none"
    assert settings.workspace_mount_roots == ""
    assert settings.allow_git_clone is False


def test_workspace_alias_names_are_stable() -> None:
    settings = _settings(
        AGENT_WORKSPACE_REGISTRY=True,
        AGENT_WORKSPACE_ROOT="/srv/ws",
        AGENT_WORKSPACE_ARCHIVE_ROOT="/srv/archive",
        AGENT_WORKSPACE_REVISIONS_KEEP=3,
        AGENT_WORKSPACE_IMPORT_MODE="mount",
        AGENT_WORKSPACE_SCAN_PROVIDER="clamav",
        AGENT_WORKSPACE_MOUNT_ROOTS="/mnt/d,/mnt/e",
        AGENT_ALLOW_GIT_CLONE=True,
    )
    assert settings.workspace_registry is True
    assert settings.workspace_root == "/srv/ws"
    assert settings.workspace_archive_root == "/srv/archive"
    assert settings.workspace_revisions_keep == 3
    assert settings.workspace_import_mode == "mount"
    assert settings.workspace_scan_provider == "clamav"
    assert settings.workspace_mount_roots == "/mnt/d,/mnt/e"
    assert settings.allow_git_clone is True


def test_policy_from_settings_normalizes_mount_roots() -> None:
    settings = _settings(
        AGENT_WORKSPACE_ROOT="/srv/ ws",
        AGENT_WORKSPACE_MOUNT_ROOTS=" /mnt/d/ , /mnt/e/projects ",
    )
    policy = WorkspacePolicy.from_settings(settings)
    assert policy.mount_roots == ("/mnt/d", "/mnt/e/projects")
    assert policy.import_mode == "copy"
    assert policy.scan_provider == "none"


def test_policy_rejects_unknown_import_mode() -> None:
    with pytest.raises(WorkspaceError):
        WorkspacePolicy(import_mode="sync")


def test_policy_rejects_unknown_scan_provider() -> None:
    with pytest.raises(WorkspaceError):
        WorkspacePolicy(scan_provider="magic")


def test_policy_rejects_negative_keep() -> None:
    with pytest.raises(WorkspaceError):
        WorkspacePolicy(revisions_keep=-1)


def test_policy_rejects_relative_mount_root() -> None:
    settings = _settings(AGENT_WORKSPACE_MOUNT_ROOTS="relative/path")
    with pytest.raises(WorkspacePathError):
        WorkspacePolicy.from_settings(settings)
