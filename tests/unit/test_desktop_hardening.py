"""M4：WSL 加固解析与探测。"""

from __future__ import annotations

from types import SimpleNamespace

from ubismart.desktop import hardening


def _report(stdout: str, *, code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=code,
        stdout=stdout.encode("utf-8"),
        stderr=b"",
    )


def test_parse_wsl_conf_sections() -> None:
    text = """
    # comment
    [interop]
    appendWindowsPath = false
    enabled=true

    [automount]
    options = "metadata"
    """
    conf = hardening.parse_wsl_conf(text)
    assert conf["interop"]["appendwindowspath"] == "false"
    assert conf["interop"]["enabled"] == "true"
    assert conf["automount"]["options"] == "metadata"


def test_evaluate_good_configuration() -> None:
    report = hardening.evaluate(
        "1000",
        {"interop": {"appendwindowspath": "false", "enabled": "false"}},
    )
    assert report.ok is True
    assert report.problems == []


def test_evaluate_root_is_problem() -> None:
    report = hardening.evaluate(
        "0", {"interop": {"appendwindowspath": "false"}}
    )
    assert report.ok is False
    assert any("root" in item for item in report.problems)


def test_evaluate_missing_append_path_is_problem() -> None:
    report = hardening.evaluate("1000", {"interop": {}})
    assert report.ok is False
    assert any("appendWindowsPath" in item for item in report.problems)


def test_evaluate_interop_warning_and_required() -> None:
    conf = {
        "interop": {
            "appendwindowspath": "false",
            "enabled": "true",
        }
    }
    warned = hardening.evaluate("1000", conf)
    assert warned.ok is True
    assert warned.warnings
    required = hardening.evaluate(
        "1000", conf, require_no_interop=True
    )
    assert required.ok is False


def test_check_parses_probe(monkeypatch) -> None:
    stdout = (
        "__UID__1000\n"
        "__CONF_START__\n"
        "[interop]\n"
        "appendWindowsPath=false\n"
        "enabled=false\n"
    )
    monkeypatch.setattr(
        hardening, "run", lambda *a, **k: _report(stdout)
    )
    report = hardening.check("Ubuntu")
    assert report.ok is True
    assert report.uid == "1000"


def test_check_probe_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        hardening,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"wsl down"
        ),
    )
    report = hardening.check("Ubuntu")
    assert report.ok is False
    assert "wsl down" in report.problems[0]
