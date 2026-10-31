"""A2L 隔离能力探测：给 doctor 与测试 skip 提供真实可用性。

每项独立 try/except；不可用只记录原因，不抛异常。结果缓存，doctor 用
`force=True` 可重新探测。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CACHE: IsolationAvailability | None = None


@dataclass(frozen=True, slots=True)
class IsolationAvailability:
    is_windows: bool = False
    job_object: bool = False
    low_integrity: bool = False
    appcontainer: bool = False
    powershell: bool = False
    tpm: bool = False
    software_attestation: bool = False
    reasons: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_windows": self.is_windows,
            "job_object": self.job_object,
            "low_integrity": self.low_integrity,
            "appcontainer": self.appcontainer,
            "powershell": self.powershell,
            "tpm": self.tpm,
            "software_attestation": self.software_attestation,
            "reasons": dict(self.reasons),
        }

    def summary(self) -> str:
        flags = [
            ("Job", self.job_object),
            ("low-integrity", self.low_integrity),
            ("AppContainer", self.appcontainer),
            ("TPM", self.tpm),
            ("software-attest", self.software_attestation),
        ]
        return " · ".join(
            f"{name}={'on' if available else 'off'}"
            for name, available in flags
        )


def _probe_job(reasons: dict[str, str]) -> bool:
    try:
        from ubismart.host_broker.win_isolation import JobObject

        job = JobObject()
        job.close()
        return True
    except Exception as exc:  # noqa: BLE001 - 探测失败按不可用
        reasons["job_object"] = f"{type(exc).__name__}: {exc}"
        return False


def _probe_low_integrity(reasons: dict[str, str]) -> bool:
    try:
        from ubismart.host_broker.win_isolation import (
            low_integrity_supported,
        )

        return low_integrity_supported()
    except Exception as exc:  # noqa: BLE001
        reasons["low_integrity"] = f"{type(exc).__name__}: {exc}"
        return False


def _probe_appcontainer(reasons: dict[str, str]) -> bool:
    try:
        from ubismart.host_broker.win_isolation import (
            AppContainerRunner,
        )

        with tempfile.TemporaryDirectory(prefix="agent-appc-probe-") as tmp:
            runner = AppContainerRunner(
                [],
                profile_name=(
                    "UBISmartProbe_" + uuid.uuid4().hex[:12]
                ),
                out_dir=Path(tmp) / "out",
            )
            runner.close()
        return True
    except Exception as exc:  # noqa: BLE001
        reasons["appcontainer"] = f"{type(exc).__name__}: {exc}"
        return False


def _probe_powershell(reasons: dict[str, str]) -> bool:
    exe = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if exe.is_file():
        return True
    reasons["powershell"] = f"找不到 {exe}"
    return False


def _probe_tpm(reasons: dict[str, str]) -> bool:
    try:
        from ubismart.host_broker.attestation import probe_tpm

        available, reason = probe_tpm()
        if not available and reason:
            reasons["tpm"] = reason
        return available
    except Exception as exc:  # noqa: BLE001
        reasons["tpm"] = f"{type(exc).__name__}: {exc}"
        return False


def _probe_software_attestation(reasons: dict[str, str]) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: F401
            Ed25519PrivateKey,
        )

        from ubismart.host_broker.attestation import default_key_path

        directory = default_key_path().parent
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".probe-{uuid.uuid4().hex}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception as exc:  # noqa: BLE001
        reasons["software_attestation"] = (
            f"{type(exc).__name__}: {exc}"
        )
        return False


def probe_isolation(*, force: bool = False) -> IsolationAvailability:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    reasons: dict[str, str] = {}
    windows = os.name == "nt"
    if not windows:
        result = IsolationAvailability(
            is_windows=False,
            reasons={
                "all": "A2L 隔离能力仅支持 Windows；Linux 使用 sandbox/WSL",
            },
        )
        _CACHE = result
        return result
    result = IsolationAvailability(
        is_windows=True,
        job_object=_probe_job(reasons),
        low_integrity=_probe_low_integrity(reasons),
        appcontainer=_probe_appcontainer(reasons),
        powershell=_probe_powershell(reasons),
        tpm=_probe_tpm(reasons),
        software_attestation=_probe_software_attestation(reasons),
        reasons=reasons,
    )
    _CACHE = result
    return result


def appcontainer_required() -> tuple[bool, str]:
    availability = probe_isolation()
    if availability.appcontainer:
        return True, ""
    return False, availability.reasons.get(
        "appcontainer", "AppContainer 不可用"
    )


__all__ = [
    "IsolationAvailability",
    "appcontainer_required",
    "probe_isolation",
]
