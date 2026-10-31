"""A3 设备台账：管理员预登记、吊销与接入校验。

台账是本地 JSON 文件，不存放私钥；A2L 只持有证书与设备证明密钥。
A3 断线/重启不丢台账；吊销后 acceptor 周期重载并断开在线连接。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REGISTRY_VERSION = 1
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"

_A2L_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class DeviceRegistryError(RuntimeError):
    pass


def default_registry_path() -> Path:
    override = os.environ.get(
        "AGENT_HOST_BROKER_DEVICE_REGISTRY", ""
    ).strip()
    if override:
        return Path(override)
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("TMPDIR")
        or "/tmp"
    )
    return Path(base) / "CodingAgent" / "host-devices.json"


def fingerprint_from_cert(path: str | Path) -> str:
    import ssl

    pem = Path(path).read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def normalize_fingerprint(value: str) -> str:
    text = str(value or "").strip().lower().replace(":", "")
    if not _FINGERPRINT_RE.fullmatch(text):
        raise DeviceRegistryError("证书指纹必须是 64 位 hex")
    return text


def normalize_a2l_id(value: str) -> str:
    text = str(value or "").strip()
    if not _A2L_ID_RE.fullmatch(text):
        raise DeviceRegistryError(f"非法 a2l_id：{value!r}")
    return text


def normalize_public_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise DeviceRegistryError("public_key 必须是 base64") from exc
    if not raw:
        raise DeviceRegistryError("public_key 不能为空")
    return base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    a2l_id: str
    fingerprint: str
    public_key: str = ""
    attestation_kind: str = ""
    label: str = ""
    status: str = STATUS_ACTIVE
    created_at: str = ""
    expires_at: str = ""
    revoked_at: str = ""
    last_seen_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "a2l_id": self.a2l_id,
            "fingerprint": self.fingerprint,
            "public_key": self.public_key,
            "attestation_kind": self.attestation_kind,
            "label": self.label,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "last_seen_at": self.last_seen_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DeviceRecord:
        return cls(
            a2l_id=normalize_a2l_id(str(payload.get("a2l_id") or "")),
            fingerprint=normalize_fingerprint(
                str(payload.get("fingerprint") or "")
            ),
            public_key=normalize_public_key(
                str(payload.get("public_key") or "")
            ),
            attestation_kind=str(
                payload.get("attestation_kind") or ""
            ).strip(),
            label=str(payload.get("label") or "").strip(),
            status=str(payload.get("status") or STATUS_ACTIVE).strip(),
            created_at=str(payload.get("created_at") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            revoked_at=str(payload.get("revoked_at") or ""),
            last_seen_at=str(payload.get("last_seen_at") or ""),
        )


@dataclass(frozen=True, slots=True)
class DeviceDecision:
    allowed: bool
    reason: str = ""
    record: DeviceRecord | None = None


class DeviceRegistry:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        enforce: bool = True,
    ) -> None:
        self.path = Path(path or default_registry_path())
        self.enforce = bool(enforce)
        self._lock = threading.Lock()
        self._devices: dict[str, DeviceRecord] = {}
        self._mtime_ns = 0
        self._size = -1
        self._load_error = ""
        self.reload(force=True)

    @property
    def load_error(self) -> str:
        with self._lock:
            return self._load_error

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------
    def reload(self, *, force: bool = False) -> bool:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            with self._lock:
                self._devices = {}
                self._mtime_ns = 0
                self._size = -1
                self._load_error = ""
            return True
        except OSError as exc:
            with self._lock:
                self._devices = {}
                self._load_error = f"无法读取设备台账：{exc}"
            return False
        with self._lock:
            if (
                not force
                and stat.st_mtime_ns == self._mtime_ns
                and stat.st_size == self._size
            ):
                return True
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("台账必须是对象")
            if int(payload.get("version") or 0) != REGISTRY_VERSION:
                raise ValueError("台账版本不匹配")
            devices: dict[str, DeviceRecord] = {}
            for item in payload.get("devices") or []:
                if not isinstance(item, dict):
                    continue
                record = DeviceRecord.from_dict(item)
                if record.a2l_id in devices:
                    raise ValueError(
                        f"重复的 a2l_id：{record.a2l_id}"
                    )
                devices[record.a2l_id] = record
        except (OSError, ValueError, TypeError) as exc:
            with self._lock:
                self._devices = {}
                self._load_error = f"设备台账无效：{exc}"
            return False
        with self._lock:
            self._devices = devices
            self._mtime_ns = stat.st_mtime_ns
            self._size = stat.st_size
            self._load_error = ""
        return True

    def reload_if_changed(self) -> bool:
        return self.reload(force=False)

    def _save_locked(self) -> None:
        payload = {
            "version": REGISTRY_VERSION,
            "devices": [
                record.to_dict()
                for record in sorted(
                    self._devices.values(), key=lambda item: item.a2l_id
                )
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if os.name != "nt":
                try:
                    temp.chmod(0o600)
                except OSError:
                    pass
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
        stat = self.path.stat()
        self._mtime_ns = stat.st_mtime_ns
        self._size = stat.st_size
        self._load_error = ""

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    # ------------------------------------------------------------------
    # 查询/管理
    # ------------------------------------------------------------------
    def list(self) -> list[DeviceRecord]:
        with self._lock:
            return sorted(
                self._devices.values(), key=lambda item: item.a2l_id
            )

    def get(self, a2l_id: str) -> DeviceRecord | None:
        with self._lock:
            return self._devices.get(str(a2l_id or ""))

    def find_by_fingerprint(
        self, fingerprint: str
    ) -> DeviceRecord | None:
        wanted = normalize_fingerprint(fingerprint)
        with self._lock:
            for record in self._devices.values():
                if record.fingerprint == wanted:
                    return record
        return None

    def register(
        self,
        *,
        a2l_id: str,
        fingerprint: str,
        public_key: str = "",
        attestation_kind: str = "",
        label: str = "",
        expires_at: str = "",
        replace_existing: bool = False,
    ) -> DeviceRecord:
        a2l_id = normalize_a2l_id(a2l_id)
        fingerprint = normalize_fingerprint(fingerprint)
        public_key = normalize_public_key(public_key)
        expires = str(expires_at or "").strip()
        if expires and _parse_time(expires) is None:
            raise DeviceRegistryError("expires_at 必须是 ISO 时间")
        with self._lock:
            current = self._devices.get(a2l_id)
            if current is not None and not replace_existing:
                if (
                    current.fingerprint != fingerprint
                    or current.status != STATUS_ACTIVE
                ):
                    raise DeviceRegistryError(
                        "设备已存在且指纹/状态不同；用 --force 覆盖"
                    )
            record = DeviceRecord(
                a2l_id=a2l_id,
                fingerprint=fingerprint,
                public_key=public_key,
                attestation_kind=str(attestation_kind or "").strip(),
                label=str(label or "").strip(),
                status=STATUS_ACTIVE,
                created_at=(
                    current.created_at if current else ""
                ) or _now(),
                expires_at=expires,
            )
            self._devices[a2l_id] = record
            self._save_locked()
            return record

    def revoke(self, identifier: str) -> DeviceRecord | None:
        text = str(identifier or "").strip()
        if not text:
            return None
        with self._lock:
            record = self._devices.get(text)
            if record is None:
                for item in self._devices.values():
                    if item.fingerprint == text.lower().replace(":", ""):
                        record = item
                        break
            if record is None:
                return None
            updated = replace(
                record,
                status=STATUS_REVOKED,
                revoked_at=_now(),
            )
            self._devices[updated.a2l_id] = updated
            self._save_locked()
            return updated

    # ------------------------------------------------------------------
    # 接入判定
    # ------------------------------------------------------------------
    def authorize(
        self,
        a2l_id: str,
        fingerprint: str,
        *,
        now: datetime | None = None,
    ) -> DeviceDecision:
        if not self.enforce:
            return DeviceDecision(allowed=True)
        if self._load_error:
            return DeviceDecision(
                allowed=False, reason=self._load_error
            )
        try:
            fingerprint = normalize_fingerprint(fingerprint)
        except DeviceRegistryError as exc:
            return DeviceDecision(allowed=False, reason=str(exc))
        with self._lock:
            record = self._devices.get(str(a2l_id or ""))
        if record is None:
            return DeviceDecision(
                allowed=False, reason="设备未登记"
            )
        if record.status != STATUS_ACTIVE:
            return DeviceDecision(
                allowed=False, reason="设备已吊销", record=record
            )
        if record.fingerprint != fingerprint:
            return DeviceDecision(
                allowed=False, reason="设备证书指纹不匹配", record=record
            )
        expiry = _parse_time(record.expires_at)
        if expiry is not None and expiry <= (now or datetime.now(UTC)):
            return DeviceDecision(
                allowed=False, reason="设备登记已过期", record=record
            )
        with self._lock:
            current = self._devices.get(record.a2l_id)
            if current is not None:
                self._devices[record.a2l_id] = replace(
                    current, last_seen_at=_now()
                )
        return DeviceDecision(allowed=True, record=record)

    def verify_attestation(
        self,
        record: DeviceRecord,
        *,
        a2l_id: str,
        nonce: str,
        evidence: dict[str, Any],
    ) -> DeviceDecision:
        if not record.public_key:
            return DeviceDecision(allowed=True, record=record)
        from coding_agent.host_broker.attestation import (
            AttestationError,
            verify_evidence,
        )

        kind = str(evidence.get("kind") or record.attestation_kind or "")
        if (
            record.attestation_kind
            and kind != record.attestation_kind
        ):
            return DeviceDecision(
                allowed=False,
                reason="设备证明类型与登记不符",
                record=record,
            )
        try:
            verify_evidence(
                kind=kind,
                public_key_b64=record.public_key,
                message=f"{nonce}:{a2l_id}",
                signature_b64=str(
                    evidence.get("signature") or ""
                ),
            )
        except AttestationError as exc:
            return DeviceDecision(
                allowed=False, reason=f"设备证明失败：{exc}", record=record
            )
        return DeviceDecision(allowed=True, record=record)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _print_devices(records: list[DeviceRecord], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                [record.to_dict() for record in records],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not records:
        print("（设备台账为空）")
        return
    for record in records:
        print(
            f"{record.a2l_id}\t{record.status}\t{record.fingerprint}\t"
            f"{record.label or '-'}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CodingAgent A2L 设备台账"
    )
    parser.add_argument(
        "--registry",
        default=str(default_registry_path()),
        help="台账文件路径",
    )
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="登记/更新设备")
    register.add_argument("--a2l-id", required=True)
    register.add_argument("--fingerprint", default="")
    register.add_argument("--cert", default="", help="从 PEM 证书算指纹")
    register.add_argument("--public-key", default="")
    register.add_argument("--kind", default="")
    register.add_argument("--label", default="")
    register.add_argument("--expires-at", default="")
    register.add_argument("--force", action="store_true")

    revoke = sub.add_parser("revoke", help="吊销设备")
    revoke.add_argument("--a2l-id", default="")
    revoke.add_argument("--fingerprint", default="")

    sub.add_parser("list", help="列出设备")
    show = sub.add_parser("show", help="查看单个设备")
    show.add_argument("--a2l-id", required=True)

    args = parser.parse_args(argv)
    registry = DeviceRegistry(args.registry, enforce=True)
    if registry.load_error:
        print(f"设备台账不可用：{registry.load_error}", file=sys.stderr)
        return 1
    try:
        if args.command == "register":
            fingerprint = args.fingerprint
            if args.cert:
                fingerprint = fingerprint_from_cert(args.cert)
            if not fingerprint:
                print(
                    "register 需要 --fingerprint 或 --cert",
                    file=sys.stderr,
                )
                return 2
            record = registry.register(
                a2l_id=args.a2l_id,
                fingerprint=fingerprint,
                public_key=args.public_key,
                attestation_kind=args.kind,
                label=args.label,
                expires_at=args.expires_at,
                replace_existing=args.force,
            )
            _print_devices([record], as_json=args.json)
            return 0
        if args.command == "revoke":
            identifier = args.a2l_id or args.fingerprint
            if not identifier:
                print(
                    "revoke 需要 --a2l-id 或 --fingerprint",
                    file=sys.stderr,
                )
                return 2
            record = registry.revoke(identifier)
            if record is None:
                print("找不到设备", file=sys.stderr)
                return 1
            _print_devices([record], as_json=args.json)
            return 0
        if args.command == "list":
            _print_devices(registry.list(), as_json=args.json)
            return 0
        if args.command == "show":
            record = registry.get(args.a2l_id)
            if record is None:
                print("找不到设备", file=sys.stderr)
                return 1
            _print_devices([record], as_json=args.json)
            return 0
    except DeviceRegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_REVOKED",
    "DeviceDecision",
    "DeviceRecord",
    "DeviceRegistry",
    "DeviceRegistryError",
    "default_registry_path",
    "fingerprint_from_cert",
    "main",
    "normalize_a2l_id",
    "normalize_fingerprint",
]
