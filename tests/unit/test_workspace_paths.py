"""RFC-0001：工作区 slug、根路径与状态机。"""

from __future__ import annotations

import os

import pytest

from ubismart.control.workspaces import (
    WorkspacePathError,
    WorkspacePolicy,
    WorkspaceStatusError,
    check_transition,
    proposed_slug,
    realpath_within,
    slugify,
    validate_root,
    workspace_root_for,
)


def test_slugify_ascii_and_mixed() -> None:
    assert slugify("My Project") == "my-project"
    assert slugify("  A__B..C  ") == "a-b-c"
    assert slugify("项目") == ""
    assert slugify("a" * 100).startswith("a" * 40)


def test_proposed_slug_falls_back_for_non_ascii() -> None:
    slug = proposed_slug("我的项目", "12345678-aaaa-bbbb")
    assert slug == "workspace"
    long_name = "x" * 100
    assert len(proposed_slug(long_name, "12345678")) <= 64


def test_workspace_root_for_builds_tenant_owner_slug() -> None:
    policy = WorkspacePolicy(root="/srv/ws")
    assert (
        workspace_root_for(policy, "tenant", "owner", "Demo")
        == "/srv/ws/tenant/owner/demo"
    )


@pytest.mark.parametrize(
    "tenant,owner,slug",
    [
        ("../etc", "owner", "demo"),
        ("tenant", "a/b", "demo"),
        ("tenant", "owner", "../../etc"),
        ("", "owner", "demo"),
        ("tenant", "..", "demo"),
    ],
)
def test_workspace_root_rejects_bad_segments(
    tenant: str, owner: str, slug: str
) -> None:
    policy = WorkspacePolicy(root="/srv/ws")
    with pytest.raises(WorkspacePathError):
        workspace_root_for(policy, tenant, owner, slug)


def test_workspace_root_requires_configured_root() -> None:
    with pytest.raises(WorkspacePathError):
        workspace_root_for(WorkspacePolicy(), "t", "o", "demo")


def test_validate_root_accepts_hosted_path_inside_root() -> None:
    policy = WorkspacePolicy(root="/srv/ws")
    root, kind, degraded = validate_root(
        policy, "/srv/ws/t/o/demo", source="upload"
    )
    assert root == "/srv/ws/t/o/demo"
    assert kind == "ext4"
    assert degraded is False


def test_validate_root_rejects_escape() -> None:
    policy = WorkspacePolicy(root="/srv/ws")
    with pytest.raises(WorkspacePathError):
        validate_root(policy, "/srv/ws/../etc", source="upload")


def test_validate_root_rejects_windows_path_for_hosted_source() -> None:
    policy = WorkspacePolicy(root="/srv/ws")
    with pytest.raises(WorkspacePathError):
        validate_root(policy, r"D:\proj", source="local_import")


def test_validate_root_rejects_mount_without_allowlist() -> None:
    policy = WorkspacePolicy(root="/srv/ws")
    with pytest.raises(WorkspacePathError):
        validate_root(policy, "/mnt/d/proj", source="local_mount")


def test_validate_root_rejects_mount_outside_allowlist() -> None:
    policy = WorkspacePolicy(root="/srv/ws", mount_roots=("/mnt/d",))
    with pytest.raises(WorkspacePathError):
        validate_root(policy, "/mnt/e/proj", source="local_mount")


def test_validate_root_accepts_mount_inside_allowlist() -> None:
    policy = WorkspacePolicy(root="/srv/ws", mount_roots=("/mnt/d",))
    root, kind, degraded = validate_root(
        policy, r"D:\proj", source="local_mount"
    )
    assert root == "/mnt/d/proj"
    assert kind == "mount"
    assert degraded is True


def test_validate_root_rejects_unknown_source() -> None:
    policy = WorkspacePolicy(root="/srv/ws")
    with pytest.raises(WorkspacePathError):
        validate_root(policy, "/srv/ws/t/o/x", source="ftp")


def test_realpath_within_is_a_noop_on_windows() -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific behavior")
    assert realpath_within("/srv/ws/t/o/x", "/srv/ws") == "/srv/ws/t/o/x"


def test_state_machine_accepts_known_transitions() -> None:
    assert check_transition("provisioning", "active") == "active"
    assert check_transition("active", "archived") == "archived"
    assert check_transition("archived", "active") == "active"
    assert check_transition("active", "deleted") == "deleted"
    assert check_transition("failed", "deleted") == "deleted"


@pytest.mark.parametrize(
    "current,target",
    [
        ("active", "provisioning"),
        ("failed", "active"),
        ("deleted", "active"),
        ("unknown", "active"),
        ("active", "unknown"),
    ],
)
def test_state_machine_rejects_illegal_transitions(
    current: str, target: str
) -> None:
    with pytest.raises(WorkspaceStatusError):
        check_transition(current, target)
