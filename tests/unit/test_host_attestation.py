"""设备证明：软件密钥、nonce 绑定、篡改拒绝与 TPM 回退。"""

from __future__ import annotations

import base64

import pytest

from coding_agent.host_broker import attestation
from coding_agent.host_broker.attestation import (
    KIND_SOFTWARE,
    AttestationError,
    AttestationUnavailable,
    SoftwareKeyIdentity,
    build_device_identity,
    verify_evidence,
)


def test_software_identity_signs_and_verifies(tmp_path) -> None:
    identity = SoftwareKeyIdentity(tmp_path / "device.key")
    evidence = identity.attest("nonce-1:a2l-1")
    assert evidence.kind == KIND_SOFTWARE
    verify_evidence(
        kind=evidence.kind,
        public_key_b64=evidence.public_key,
        message="nonce-1:a2l-1",
        signature_b64=evidence.signature,
    )
    with pytest.raises(AttestationError):
        verify_evidence(
            kind=evidence.kind,
            public_key_b64=evidence.public_key,
            message="nonce-2:a2l-1",
            signature_b64=evidence.signature,
        )


def test_software_key_is_persistent(tmp_path) -> None:
    path = tmp_path / "device.key"
    first = SoftwareKeyIdentity(path)
    second = SoftwareKeyIdentity(path)
    assert first.public_key() == second.public_key()


def test_tampered_signature_is_rejected(tmp_path) -> None:
    identity = SoftwareKeyIdentity(tmp_path / "device.key")
    evidence = identity.attest("n:1")
    raw = bytearray(base64.b64decode(evidence.signature))
    raw[0] ^= 0xFF
    with pytest.raises(AttestationError):
        verify_evidence(
            kind=evidence.kind,
            public_key_b64=evidence.public_key,
            message="n:1",
            signature_b64=base64.b64encode(raw).decode("ascii"),
        )


def test_auto_falls_back_to_software_when_tpm_fails(
    tmp_path, monkeypatch
) -> None:
    def _fail(a2l_id):
        raise AttestationUnavailable("no tpm")

    monkeypatch.setattr(attestation, "TpmKeyIdentity", _fail)
    identity = build_device_identity(
        "auto", a2l_id="a2l-1", key_path=tmp_path / "device.key"
    )
    assert identity.kind == KIND_SOFTWARE


def test_tpm_mode_does_not_fall_back(tmp_path, monkeypatch) -> None:
    def _fail(a2l_id):
        raise AttestationUnavailable("no tpm")

    monkeypatch.setattr(attestation, "TpmKeyIdentity", _fail)
    with pytest.raises(AttestationUnavailable):
        build_device_identity(
            "tpm", a2l_id="a2l-1", key_path=tmp_path / "device.key"
        )
