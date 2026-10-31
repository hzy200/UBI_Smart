"""A3 设备台账：登记、吊销、过期、重载与 fail-closed。"""

from __future__ import annotations

import hashlib
import json

import pytest

from ubismart.host_broker import devices
from ubismart.host_broker.devices import (
    STATUS_ACTIVE,
    STATUS_REVOKED,
    DeviceRegistry,
    DeviceRegistryError,
)


def _fp(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_register_authorize_and_revoke(tmp_path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.json")
    fingerprint = _fp("cert-a")
    record = registry.register(
        a2l_id="a2l-1",
        fingerprint=fingerprint,
        label="alice-laptop",
    )
    assert record.status == STATUS_ACTIVE
    decision = registry.authorize("a2l-1", fingerprint)
    assert decision.allowed is True
    assert decision.record.a2l_id == "a2l-1"

    assert registry.authorize("missing", fingerprint).allowed is False
    assert (
        registry.authorize("a2l-1", _fp("other")).allowed is False
    )

    revoked = registry.revoke("a2l-1")
    assert revoked is not None and revoked.status == STATUS_REVOKED
    assert registry.authorize("a2l-1", fingerprint).allowed is False
    reloaded = DeviceRegistry(tmp_path / "devices.json")
    assert reloaded.get("a2l-1").status == STATUS_REVOKED


def test_register_requires_force_for_changed_fingerprint(tmp_path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.json")
    registry.register(a2l_id="a2l-1", fingerprint=_fp("a"))
    with pytest.raises(DeviceRegistryError):
        registry.register(a2l_id="a2l-1", fingerprint=_fp("b"))
    updated = registry.register(
        a2l_id="a2l-1",
        fingerprint=_fp("b"),
        replace_existing=True,
    )
    assert updated.fingerprint == _fp("b")


def test_expired_device_is_denied(tmp_path) -> None:
    registry = DeviceRegistry(tmp_path / "devices.json")
    fingerprint = _fp("cert")
    registry.register(
        a2l_id="a2l-1",
        fingerprint=fingerprint,
        expires_at="2000-01-01T00:00:00+00:00",
    )
    decision = registry.authorize("a2l-1", fingerprint)
    assert decision.allowed is False
    assert "过期" in decision.reason


def test_reload_picks_up_external_changes(tmp_path) -> None:
    path = tmp_path / "devices.json"
    registry = DeviceRegistry(path)
    registry.register(a2l_id="a2l-1", fingerprint=_fp("a"))

    payload = {
        "version": devices.REGISTRY_VERSION,
        "devices": [
            {
                "a2l_id": "a2l-1",
                "fingerprint": _fp("a"),
                "status": "revoked",
                "created_at": "2026-01-01T00:00:00+00:00",
                "revoked_at": "2026-01-02T00:00:00+00:00",
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert registry.reload_if_changed() is True
    assert registry.authorize("a2l-1", _fp("a")).allowed is False


def test_corrupt_registry_fails_closed(tmp_path) -> None:
    path = tmp_path / "devices.json"
    registry = DeviceRegistry(path)
    registry.register(a2l_id="a2l-1", fingerprint=_fp("a"))
    path.write_text("{not-json", encoding="utf-8")
    assert registry.reload(force=True) is False
    decision = registry.authorize("a2l-1", _fp("a"))
    assert decision.allowed is False
    assert "台账" in decision.reason


def test_devices_cli_roundtrip(tmp_path, capsys) -> None:
    path = str(tmp_path / "devices.json")
    assert (
        devices.main(
            [
                "--registry",
                path,
                "register",
                "--a2l-id",
                "a2l-1",
                "--fingerprint",
                _fp("a"),
                "--label",
                "lab",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        devices.main(
            ["--registry", path, "--json", "list"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["a2l_id"] == "a2l-1"

    assert (
        devices.main(
            ["--registry", path, "revoke", "--a2l-id", "a2l-1"]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        devices.main(
            ["--registry", path, "--json", "show", "--a2l-id", "a2l-1"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["status"] == STATUS_REVOKED


def test_registry_verifies_device_attestation(tmp_path) -> None:
    from ubismart.host_broker.attestation import SoftwareKeyIdentity

    identity = SoftwareKeyIdentity(tmp_path / "device.key")
    evidence = identity.attest("nonce-1:a2l-1")
    registry = DeviceRegistry(tmp_path / "devices.json")
    registry.register(
        a2l_id="a2l-1",
        fingerprint=_fp("cert"),
        public_key=evidence.public_key,
        attestation_kind=evidence.kind,
    )
    record = registry.get("a2l-1")
    assert record is not None
    accepted = registry.verify_attestation(
        record,
        a2l_id="a2l-1",
        nonce="nonce-1",
        evidence={
            "kind": evidence.kind,
            "signature": evidence.signature,
        },
    )
    assert accepted.allowed is True
    wrong_nonce = registry.verify_attestation(
        record,
        a2l_id="a2l-1",
        nonce="nonce-2",
        evidence={
            "kind": evidence.kind,
            "signature": evidence.signature,
        },
    )
    assert wrong_nonce.allowed is False
    wrong_kind = registry.verify_attestation(
        record,
        a2l_id="a2l-1",
        nonce="nonce-1",
        evidence={
            "kind": "tpm2-ecdsa-p256",
            "signature": evidence.signature,
        },
    )
    assert wrong_kind.allowed is False
