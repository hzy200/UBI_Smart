"""A2L 签名策略：签名、验签、绑定、过期与 CLI 覆盖。"""

from __future__ import annotations

import argparse
import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from coding_agent.host_broker import policy_doc
from coding_agent.host_broker.__main__ import _apply_policy
from coding_agent.host_broker.policy_doc import (
    PolicyError,
    load_signed_policy,
    sign_payload,
)


def _keypair():
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.b64encode(raw).decode("ascii")


def _write_policy(tmp_path, payload, private):
    envelope = sign_payload(payload, private)
    path = tmp_path / "policy.signed.json"
    path.write_text(
        json.dumps(envelope, ensure_ascii=False), encoding="utf-8"
    )
    return path


def test_sign_and_verify_policy(tmp_path) -> None:
    private, public = _keypair()
    payload = {
        "version": 1,
        "a2l_id": "a2l-1",
        "allow_write": True,
        "allow_exec": False,
        "job_max_processes": 16,
        "toolchains": [
            {
                "id": "python-3.11",
                "kind": "python",
                "root": str(tmp_path),
                "commands": ["python"],
                "output_roots": ["build"],
            }
        ],
    }
    path = _write_policy(tmp_path, payload, private)
    document = load_signed_policy(path, public)
    assert document.allow_write is True
    assert document.allow_exec is False
    assert document.job_max_processes == 16
    document.check(a2l_id="a2l-1")


def test_tampered_policy_is_rejected(tmp_path) -> None:
    private, public = _keypair()
    path = _write_policy(
        tmp_path, {"version": 1, "allow_write": False}, private
    )
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["allow_write"] = True
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(PolicyError):
        load_signed_policy(path, public)


def test_expired_and_wrong_a2l_are_rejected(tmp_path) -> None:
    private, public = _keypair()
    expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    path = _write_policy(
        tmp_path,
        {
            "version": 1,
            "a2l_id": "a2l-1",
            "expires_at": expired,
        },
        private,
    )
    document = load_signed_policy(path, public)
    with pytest.raises(PolicyError):
        document.check(a2l_id="a2l-1")
    with pytest.raises(PolicyError):
        document.check(a2l_id="other")


def test_policy_overrides_cli_flags(tmp_path) -> None:
    private, public = _keypair()
    path = _write_policy(
        tmp_path,
        {
            "version": 1,
            "a2l_id": "a2l-1",
            "allow_write": False,
            "allow_exec": True,
            "job_max_processes": 8,
        },
        private,
    )
    args = argparse.Namespace(
        broker_id="a2l-1",
        policy_file=str(path),
        policy_pubkey=public,
        policy_pubkey_file="",
        allow_write=True,
        allow_exec=False,
        job_max_processes=32,
        job_memory_mb=2048,
        attestation="off",
        toolchain_json=[],
    )
    document = _apply_policy(args)
    assert document is not None
    assert args.allow_write is False
    assert args.allow_exec is True
    assert args.job_max_processes == 8


def test_policy_cli_keygen_sign_verify(tmp_path, capsys) -> None:
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    assert (
        policy_doc.main(
            [
                "keygen",
                "--private",
                str(private_path),
                "--public",
                str(public_path),
            ]
        )
        == 0
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps({"version": 1, "allow_write": False}),
        encoding="utf-8",
    )
    signed = tmp_path / "signed.json"
    assert (
        policy_doc.main(
            [
                "sign",
                "--key",
                str(private_path),
                "--in",
                str(policy_path),
                "--out",
                str(signed),
            ]
        )
        == 0
    )
    assert (
        policy_doc.main(
            [
                "verify",
                "--public",
                str(public_path),
                "--in",
                str(signed),
            ]
        )
        == 0
    )
    assert "allow_write" in capsys.readouterr().out
