"""隔离能力探测：Linux 明确不可用，Windows 不抛异常。"""

from __future__ import annotations

import os

from ubismart.host_broker.availability import (
    appcontainer_required,
    probe_isolation,
)


def test_probe_is_stable_and_cached() -> None:
    first = probe_isolation(force=True)
    second = probe_isolation()
    assert first == second


def test_non_windows_reports_unavailable() -> None:
    availability = probe_isolation(force=True)
    if os.name == "nt":
        assert availability.is_windows is True
        assert availability.job_object is True
        # AppContainer/TPM 因机器而异，只要求不抛异常、原因可读。
        assert isinstance(availability.appcontainer, bool)
        assert isinstance(availability.tpm, bool)
    else:
        assert availability.is_windows is False
        assert availability.job_object is False
        assert availability.appcontainer is False
        assert availability.tpm is False
        available, reason = appcontainer_required()
        assert available is False
        assert reason
