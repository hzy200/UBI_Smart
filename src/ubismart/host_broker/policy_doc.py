"""A2L 运行策略：Ed25519 签名信封 + 本地验签。

策略由企业根密钥签名，A2L 用固定公钥验证后才应用。它约束写/exec/
toolchain/作业限额，不能替代本地 grant 与同意。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

POLICY_VERSION = 1
_ALLOWED_ATTESTATION = {"off", "auto", "require", "tpm"}


class PolicyError(RuntimeError):
    pass


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC)
        if parsed.tzinfo is None
        else parsed.astimezone(UTC)
    )


def _raw_public_key(text: str) -> bytes | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        raw = bytes.fromhex(stripped)
        if len(raw) == 32:
            return raw
    except ValueError:
        pass
    try:
        raw = base64.b64decode(stripped, validate=True)
    except (ValueError, binascii.Error):
        return None
    return raw if len(raw) == 32 else None


def load_public_key(source: str) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    text = str(source or "").strip()
    if not text:
        raise PolicyError("未配置策略公钥")
    path = Path(text)
    if path.is_file():
        data = path.read_bytes()
        try:
            key = serialization.load_pem_public_key(data)
        except (ValueError, TypeError):
            key = None
        if key is not None:
            if not isinstance(key, Ed25519PublicKey):
                raise PolicyError("策略公钥必须是 Ed25519")
            return key
        raw = _raw_public_key(data.decode("ascii", errors="ignore"))
        if raw is None:
            raise PolicyError("无法解析策略公钥文件")
        return Ed25519PublicKey.from_public_bytes(raw)
    raw = _raw_public_key(text)
    if raw is None:
        raise PolicyError("策略公钥必须是 PEM、base64 或 hex")
    return Ed25519PublicKey.from_public_bytes(raw)


def load_private_key(path: str) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    try:
        key = serialization.load_pem_private_key(
            Path(path).read_bytes(), password=None
        )
    except (OSError, ValueError, TypeError) as exc:
        raise PolicyError(f"无法读取签名私钥：{exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PolicyError("签名私钥必须是 Ed25519")
    return key


def sign_payload(payload: dict[str, Any], private_key: Any) -> dict[str, Any]:
    signature = private_key.sign(_canonical(payload))
    return {
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
        "key_id": str(payload.get("key_id") or ""),
    }


@dataclass(frozen=True, slots=True)
class PolicyDocument:
    version: int = POLICY_VERSION
    a2l_id: str = ""
    issued_at: str = ""
    expires_at: str = ""
    allow_write: bool | None = None
    allow_exec: bool | None = None
    toolchains: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    job_max_processes: int | None = None
    job_memory_mb: int | None = None
    attestation: str | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PolicyDocument:
        if not isinstance(payload, dict):
            raise PolicyError("策略 payload 必须是对象")
        version = int(payload.get("version") or 0)
        if version != POLICY_VERSION:
            raise PolicyError(f"策略版本不支持：{version}")
        toolchains = payload.get("toolchains") or []
        if not isinstance(toolchains, list) or any(
            not isinstance(item, dict) for item in toolchains
        ):
            raise PolicyError("toolchains 必须是对象数组")
        attestation = payload.get("attestation")
        if attestation is not None:
            attestation = str(attestation).strip().lower()
            if attestation not in _ALLOWED_ATTESTATION:
                raise PolicyError("attestation 取值非法")
        return cls(
            version=version,
            a2l_id=str(payload.get("a2l_id") or "").strip(),
            issued_at=str(payload.get("issued_at") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            allow_write=_optional_bool(payload, "allow_write"),
            allow_exec=_optional_bool(payload, "allow_exec"),
            toolchains=tuple(dict(item) for item in toolchains),
            job_max_processes=_optional_int(payload, "job_max_processes"),
            job_memory_mb=_optional_int(payload, "job_memory_mb"),
            attestation=attestation,
            notes=str(payload.get("notes") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self.version,
            "toolchains": [dict(item) for item in self.toolchains],
        }
        for key in ("a2l_id", "issued_at", "expires_at", "notes"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        for key in (
            "allow_write",
            "allow_exec",
            "job_max_processes",
            "job_memory_mb",
            "attestation",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload

    def check(self, *, a2l_id: str, now: datetime | None = None) -> None:
        if self.a2l_id and self.a2l_id != str(a2l_id or ""):
            raise PolicyError("策略绑定的 a2l_id 不匹配")
        expires = _parse_time(self.expires_at)
        if expires is not None and expires <= (now or datetime.now(UTC)):
            raise PolicyError("策略已过期")
        if self.job_max_processes is not None and not (
            1 <= self.job_max_processes <= 1024
        ):
            raise PolicyError("job_max_processes 越界")
        if self.job_memory_mb is not None and not (
            64 <= self.job_memory_mb <= 65536
        ):
            raise PolicyError("job_memory_mb 越界")


def _optional_bool(payload: dict[str, Any], key: str) -> bool | None:
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if not isinstance(value, bool):
        raise PolicyError(f"{key} 必须是布尔值")
    return value


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    if key not in payload or payload[key] is None:
        return None
    try:
        return int(payload[key])
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{key} 必须是整数") from exc


def load_signed_policy(
    path: str | Path, public_key_source: str
) -> PolicyDocument:
    from cryptography.exceptions import InvalidSignature

    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"无法读取策略文件：{exc}") from exc
    if not isinstance(envelope, dict):
        raise PolicyError("策略文件必须是对象")
    payload = envelope.get("payload")
    signature_b64 = str(envelope.get("signature") or "")
    if not isinstance(payload, dict) or not signature_b64:
        raise PolicyError("策略文件缺少 payload/signature")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PolicyError("策略签名不是 base64") from exc
    key = load_public_key(public_key_source)
    try:
        key.verify(signature, _canonical(payload))
    except InvalidSignature as exc:
        raise PolicyError("策略签名无效") from exc
    return PolicyDocument.from_dict(payload)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _cmd_keygen(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    for path, data in (
        (args.private, private_pem),
        (args.public, public_pem),
    ):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if os.name != "nt":
            try:
                target.chmod(0o600)
            except OSError:
                pass
    print(f"private={args.private}")
    print(f"public={args.public}")
    return 0


def _cmd_sign(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"无法读取策略：{exc}", file=sys.stderr)
        return 2
    if isinstance(payload, dict) and isinstance(
        payload.get("payload"), dict
    ):
        payload = payload["payload"]
    if not isinstance(payload, dict):
        print("策略必须是 JSON 对象", file=sys.stderr)
        return 2
    try:
        PolicyDocument.from_dict(payload)
        envelope = sign_payload(payload, load_private_key(args.key))
    except PolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    Path(args.output).write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"signed={args.output}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        document = load_signed_policy(args.file, args.public)
        if args.a2l_id:
            document.check(a2l_id=str(args.a2l_id))
        else:
            expires = _parse_time(document.expires_at)
            if expires is not None and expires <= datetime.now(UTC):
                raise PolicyError("策略已过期")
    except PolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(document.to_dict(), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="UBISmart A2L 签名策略"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="生成 Ed25519 密钥对")
    keygen.add_argument("--private", required=True)
    keygen.add_argument("--public", required=True)
    keygen.set_defaults(func=_cmd_keygen)

    sign = sub.add_parser("sign", help="签名策略 JSON")
    sign.add_argument("--key", required=True)
    sign.add_argument("--in", dest="input", required=True)
    sign.add_argument("--out", dest="output", required=True)
    sign.set_defaults(func=_cmd_sign)

    verify = sub.add_parser("verify", help="验证签名策略")
    verify.add_argument("--public", required=True)
    verify.add_argument("--in", dest="file", required=True)
    verify.add_argument("--a2l-id", default="")
    verify.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return int(args.func(args))


__all__ = [
    "POLICY_VERSION",
    "PolicyDocument",
    "PolicyError",
    "load_private_key",
    "load_public_key",
    "load_signed_policy",
    "main",
    "sign_payload",
]
