"""RFC-0001：扫描 provider 与失败策略。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ubismart.config import Settings
from ubismart.workspace import scan as scan_module
from ubismart.workspace.scan import (
    ClamavScanner,
    NoopScanner,
    ScanUnavailable,
    build_scanner,
)


def _settings(**kwargs) -> Settings:
    data = {"_env_file": None}
    data.update(kwargs)
    return Settings(**data)


def test_noop_returns_skipped() -> None:
    assert NoopScanner().scan("/tmp").status == "skipped"


def test_build_scanner_selects_provider() -> None:
    assert isinstance(
        build_scanner(_settings(workspace_scan_provider="none")),
        NoopScanner,
    )
    assert isinstance(
        build_scanner(_settings(workspace_scan_provider="clamav")),
        ClamavScanner,
    )


def test_clamav_missing_binary_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(scan_module.shutil, "which", lambda _name: None)
    with pytest.raises(ScanUnavailable):
        ClamavScanner().scan("/tmp")


def test_clamav_status_mapping(monkeypatch) -> None:
    monkeypatch.setattr(
        scan_module.shutil, "which", lambda _name: "/usr/bin/clamscan"
    )

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scan_module.subprocess, "run", fake_run)
    assert ClamavScanner().scan("/tmp").status == "clean"

    monkeypatch.setattr(
        scan_module.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1, stdout="FOUND", stderr=""
        ),
    )
    result = ClamavScanner().scan("/tmp")
    assert result.status == "warn"
    assert "FOUND" in result.detail

    monkeypatch.setattr(
        scan_module.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=2, stdout="error", stderr=""
        ),
    )
    with pytest.raises(ScanUnavailable):
        ClamavScanner().scan("/tmp")


def test_clamav_timeout_is_unavailable(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(
        scan_module.shutil, "which", lambda _name: "/usr/bin/clamscan"
    )

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("clamscan", 1)

    monkeypatch.setattr(scan_module.subprocess, "run", timeout)
    with pytest.raises(ScanUnavailable):
        ClamavScanner().scan("/tmp")
