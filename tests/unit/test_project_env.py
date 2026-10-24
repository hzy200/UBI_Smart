"""Phase 3：工作区/项目 Python 只读探测。"""

from __future__ import annotations

from coding_agent import project_env
from coding_agent.project_env import probe_project, workspace_mode


def test_workspace_mode_classifies_native_and_mnt() -> None:
    assert workspace_mode("/home/me/proj") == "native"
    assert workspace_mode("/mnt/d/proj") == "mnt"
    assert workspace_mode("/mnt") == "mnt"
    assert workspace_mode("D:\\proj") == "other"
    assert workspace_mode("") == "other"


def test_mnt_workspace_is_flagged_as_slow(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(project_env, "_version", lambda python: "3.12.4")
    monkeypatch.setattr(project_env, "_has_pytest", lambda python: True)
    data = probe_project("/mnt/d/proj")
    assert data["mode"] == "mnt"
    assert any("/mnt" in item for item in data["warnings"])


def test_project_venv_is_preferred_over_system_python(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "proj"
    python = workspace / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)

    monkeypatch.setattr(project_env, "_version", lambda path: "3.12.4")
    monkeypatch.setattr(project_env, "_has_pytest", lambda path: True)

    data = probe_project(str(workspace), agent_python="/opt/agent/python")
    assert data["project"]["source"] == ".venv"
    assert data["project"]["python"] == str(python)
    assert data["project"]["version"] == "3.12.4"
    assert data["project"]["pytest"] is True
    assert data["test_command"].endswith("-m pytest -q")
    assert data["agent_python"] == "/opt/agent/python"
    assert data["warnings"] == []


def test_missing_venv_warns_and_falls_back_to_system_python(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    monkeypatch.setattr(
        project_env.shutil, "which", lambda name: "/usr/bin/python3"
    )
    monkeypatch.setattr(project_env, "_version", lambda path: "3.11.9")
    monkeypatch.setattr(project_env, "_has_pytest", lambda path: False)

    data = probe_project(str(workspace))
    assert data["project"]["source"] == "system"
    assert data["project"]["python"] == "/usr/bin/python3"
    assert data["test_command"] == ""
    assert any("虚拟环境" in item for item in data["warnings"])
    assert any("pytest" in item for item in data["warnings"])


def test_package_manager_uses_most_specific_lockfile(tmp_path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("", encoding="utf-8")
    (workspace / "requirements.txt").write_text("", encoding="utf-8")
    data = probe_project(str(workspace))
    assert (data["package_manager"], data["lockfile"]) == ("pip", "pyproject.toml")

    (workspace / "uv.lock").write_text("", encoding="utf-8")
    data = probe_project(str(workspace))
    assert (data["package_manager"], data["lockfile"]) == ("uv", "uv.lock")

    (workspace / "poetry.lock").write_text("", encoding="utf-8")
    data = probe_project(str(workspace))
    assert (data["package_manager"], data["lockfile"]) == ("uv", "uv.lock")


def test_missing_workspace_is_reported_without_raising(tmp_path) -> None:
    data = probe_project(str(tmp_path / "nope"))
    assert data["exists"] is False
    assert data["writable"] is False
    assert any("不存在" in item for item in data["warnings"])
