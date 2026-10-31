# ruff: noqa: E501 - 内嵌 PowerShell 脚本按可读性保留长行
"""设备证明：对连接 nonce 签名，证明持有登记的私钥。

优先尝试 Windows TPM（CNG Platform Crypto Provider，ECDSA P-256）；
不可用时回退到持久软件 Ed25519 密钥，并在 evidence 中如实标注类型。
这不是 PCR quote / 远程启动证明，只证明密钥驻留与持有。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

ATTESTATION_OFF = "off"
ATTESTATION_AUTO = "auto"
ATTESTATION_REQUIRE = "require"

KIND_SOFTWARE = "software-ed25519"
KIND_TPM = "tpm2-ecdsa-p256"


class AttestationError(RuntimeError):
    pass


class AttestationUnavailable(AttestationError):
    pass


def default_key_path() -> Path:
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("TMPDIR")
        or "/tmp"
    )
    return Path(base) / "CodingAgent" / "device-attestation.key"


@dataclass(frozen=True, slots=True)
class AttestationEvidence:
    kind: str
    public_key: str
    signature: str

    def to_meta(self) -> dict[str, str]:
        return {
            "attestation_kind": self.kind,
            "device_public_key": self.public_key,
            "attestation_signature": self.signature,
        }


class DeviceIdentity:
    kind = ""

    def public_key(self) -> str:
        raise NotImplementedError

    def attest(self, message: str) -> AttestationEvidence:
        raise NotImplementedError


class SoftwareKeyIdentity(DeviceIdentity):
    kind = KIND_SOFTWARE

    def __init__(self, key_path: str | Path | None = None) -> None:
        self.key_path = Path(key_path or default_key_path())
        self._lock = threading.Lock()
        self._private = self._load_or_create()

    def _load_or_create(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = self.key_path.read_bytes()
        except FileNotFoundError:
            data = b""
        except OSError as exc:
            raise AttestationUnavailable(
                f"无法读取设备证明密钥：{exc}"
            ) from exc
        if data:
            try:
                return serialization.load_pem_private_key(
                    data, password=None
                )
            except (ValueError, TypeError) as exc:
                raise AttestationUnavailable(
                    f"设备证明密钥无效：{exc}"
                ) from exc
        private = Ed25519PrivateKey.generate()
        pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        temp = self.key_path.with_name(
            f".{self.key_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp.write_bytes(pem)
            if os.name != "nt":
                try:
                    temp.chmod(0o600)
                except OSError:
                    pass
            os.replace(temp, self.key_path)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
        return private

    def public_key(self) -> str:
        from cryptography.hazmat.primitives import serialization

        raw = self._private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")

    def attest(self, message: str) -> AttestationEvidence:
        with self._lock:
            signature = self._private.sign(message.encode("utf-8"))
        return AttestationEvidence(
            kind=self.kind,
            public_key=self.public_key(),
            signature=base64.b64encode(signature).decode("ascii"),
        )


def _powershell_exe() -> str:
    return str(
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )


def _run_powershell(script: str, *, timeout: float = 30.0) -> dict[str, Any]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            [
                _powershell_exe(),
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AttestationUnavailable(f"PowerShell 不可用：{exc}") from exc
    if result.returncode != 0:
        raise AttestationUnavailable(
            f"TPM 证明失败：{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise AttestationUnavailable(
            f"TPM 证明输出不可解析：{result.stdout[:200]}"
        ) from exc


class TpmKeyIdentity(DeviceIdentity):
    kind = KIND_TPM

    def __init__(self, a2l_id: str) -> None:
        if os.name != "nt":
            raise AttestationUnavailable("TPM 仅支持 Windows")
        digest = hashlib.sha256(
            str(a2l_id or "").encode("utf-8")
        ).hexdigest()[:16]
        self.key_name = f"CodingAgent-A2L-{digest}"
        self._lock = threading.Lock()
        self._public = self._ensure_key()

    def _ensure_key(self) -> str:
        name = json.dumps(self.key_name)
        script = f"""$ErrorActionPreference='Stop'
$name={name}
$provider=New-Object System.Security.Cryptography.CngProvider('Microsoft Platform Crypto Provider')
try {{
  $key=[System.Security.Cryptography.CngKey]::Open($name, $provider, [System.Security.Cryptography.CngKeyOpenOptions]::None)
}} catch {{
  $params=New-Object System.Security.Cryptography.CngKeyCreationParameters
  $params.Provider=$provider
  $params.KeyUsage=[System.Security.Cryptography.CngKeyUsages]::Signing
  $params.ExportPolicy=[System.Security.Cryptography.CngExportPolicies]::None
  $key=[System.Security.Cryptography.CngKey]::Create([System.Security.Cryptography.CngAlgorithm]::ECDsaP256, $name, $params)
}}
$ecdsa=New-Object System.Security.Cryptography.ECDsaCng($key)
$pub=$ecdsa.ExportParameters($false)
@{{
  x=[Convert]::ToBase64String($pub.Q.X);
  y=[Convert]::ToBase64String($pub.Q.Y)
}} | ConvertTo-Json -Compress
"""
        data = _run_powershell(script)
        try:
            x = base64.b64decode(str(data["x"]), validate=True)
            y = base64.b64decode(str(data["y"]), validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise AttestationUnavailable(
                "TPM 公钥输出格式错误"
            ) from exc
        if len(x) != 32 or len(y) != 32:
            raise AttestationUnavailable("TPM 公钥长度错误")
        return base64.b64encode(b"\x04" + x + y).decode("ascii")

    def public_key(self) -> str:
        return self._public

    def attest(self, message: str) -> AttestationEvidence:
        name = json.dumps(self.key_name)
        payload = base64.b64encode(
            message.encode("utf-8")
        ).decode("ascii")
        script = f"""$ErrorActionPreference='Stop'
$name={name}
$data=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))
$provider=New-Object System.Security.Cryptography.CngProvider('Microsoft Platform Crypto Provider')
$key=[System.Security.Cryptography.CngKey]::Open($name, $provider, [System.Security.Cryptography.CngKeyOpenOptions]::None)
$ecdsa=New-Object System.Security.Cryptography.ECDsaCng($key)
$sig=$ecdsa.SignData([Text.Encoding]::UTF8.GetBytes($data))
@{{
  signature=[Convert]::ToBase64String($sig)
}} | ConvertTo-Json -Compress
"""
        with self._lock:
            data = _run_powershell(script)
        try:
            signature = str(data["signature"])
        except (KeyError, TypeError) as exc:
            raise AttestationUnavailable(
                "TPM 签名输出格式错误"
            ) from exc
        return AttestationEvidence(
            kind=self.kind,
            public_key=self.public_key(),
            signature=signature,
        )


def probe_tpm() -> tuple[bool, str]:
    """尝试创建并删除一个临时 CNG/TPM 密钥；不留下持久状态。"""
    if os.name != "nt":
        return False, "仅 Windows"
    name = f"UBISmartProbe-{uuid.uuid4().hex[:8]}"
    script = f"""$ErrorActionPreference='Stop'
$name={json.dumps(name)}
$ok=$false
$err=''
$key=$null
try {{
  $provider=New-Object System.Security.Cryptography.CngProvider('Microsoft Platform Crypto Provider')
  $params=New-Object System.Security.Cryptography.CngKeyCreationParameters
  $params.Provider=$provider
  $params.KeyUsage=[System.Security.Cryptography.CngKeyUsages]::Signing
  $params.ExportPolicy=[System.Security.Cryptography.CngExportPolicies]::None
  $key=[System.Security.Cryptography.CngKey]::Create([System.Security.Cryptography.CngAlgorithm]::ECDsaP256, $name, $params)
  $ok=$true
}} catch {{
  $err=$_.Exception.Message
}}
if ($key -ne $null) {{ try {{ $key.Delete() }} catch {{}} }}
@{{
  ok=$ok;
  error=$err
}} | ConvertTo-Json -Compress
"""
    try:
        data = _run_powershell(script)
    except AttestationUnavailable as exc:
        return False, str(exc)
    return bool(data.get("ok")), str(data.get("error") or "")


def build_device_identity(
    mode: str,
    *,
    a2l_id: str,
    key_path: str | Path | None = None,
) -> DeviceIdentity | None:
    mode = str(mode or ATTESTATION_OFF).strip().lower()
    if mode == ATTESTATION_OFF or not mode:
        return None
    if mode not in {ATTESTATION_AUTO, ATTESTATION_REQUIRE, "tpm"}:
        raise AttestationUnavailable(f"未知 attestation 模式：{mode}")
    try:
        return TpmKeyIdentity(a2l_id)
    except AttestationUnavailable as exc:
        if mode == "tpm":
            raise
        _LOG.warning("TPM 不可用，回退软件设备密钥：%s", exc)
    return SoftwareKeyIdentity(key_path)


def verify_evidence(
    *,
    kind: str,
    public_key_b64: str,
    message: str,
    signature_b64: str,
) -> None:
    try:
        public = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise AttestationError("公钥或签名不是 base64") from exc
    kind = str(kind or "").lower()
    data = message.encode("utf-8")
    try:
        if "ed25519" in kind:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )

            try:
                Ed25519PublicKey.from_public_bytes(public).verify(
                    signature, data
                )
            except InvalidSignature as exc:
                raise AttestationError("设备签名无效") from exc
            return
        if "ecdsa" in kind or "tpm" in kind:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.asymmetric.utils import (
                encode_dss_signature,
            )

            key = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), public
            )
            if len(signature) == 64:
                r = int.from_bytes(signature[:32], "big")
                s = int.from_bytes(signature[32:], "big")
                signature = encode_dss_signature(r, s)
            try:
                key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            except InvalidSignature as exc:
                raise AttestationError("设备签名无效") from exc
            return
    except (ValueError, TypeError) as exc:
        raise AttestationError(f"设备公钥无效：{exc}") from exc
    raise AttestationError(f"未知证明类型：{kind!r}")


__all__ = [
    "ATTESTATION_AUTO",
    "ATTESTATION_OFF",
    "ATTESTATION_REQUIRE",
    "KIND_SOFTWARE",
    "KIND_TPM",
    "AttestationError",
    "AttestationEvidence",
    "AttestationUnavailable",
    "DeviceIdentity",
    "SoftwareKeyIdentity",
    "TpmKeyIdentity",
    "build_device_identity",
    "default_key_path",
    "verify_evidence",
]
