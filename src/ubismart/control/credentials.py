"""上游 provider Key 的集中加密存储。

主密钥来自 `AGENT_SECRET_MASTER_KEY`（base64/hex 32B）或文件；密文用
AES-256-GCM，AAD 绑定 (tenant, user, provider)，避免跨用户搬移密文。
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ubismart.control.schema import ControlProviderKey
from ubismart.control.store import _now


class CredentialError(RuntimeError):
    pass


def _decode_key(raw: str) -> bytes:
    text = str(raw or "").strip()
    if not text:
        return b""
    if len(text) == 64:
        try:
            value = bytes.fromhex(text)
            if len(value) == 32:
                return value
        except ValueError:
            pass
    try:
        value = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialError("主密钥必须是 base64 或 64 位 hex") from exc
    if len(value) != 32:
        raise CredentialError("主密钥必须是 32 字节")
    return value


def load_master_key(
    raw: str = "", path: str = ""
) -> bytes:
    if raw:
        return _decode_key(raw)
    target = str(path or "").strip()
    if target:
        try:
            text = Path(target).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CredentialError(f"无法读取主密钥文件：{exc}") from exc
        return _decode_key(text)
    # 允许测试/开发环境用环境变量直接注入。
    return _decode_key(os.environ.get("AGENT_SECRET_MASTER_KEY", ""))


class CredentialStore:
    def __init__(
        self,
        engine: Any,
        key: bytes,
        *,
        key_version: int = 1,
    ) -> None:
        self.engine = engine
        self.key = key
        self.key_version = max(1, int(key_version or 1))

    @classmethod
    def from_settings(cls, engine: Any, settings: Any) -> CredentialStore:
        key = load_master_key(
            getattr(settings, "secret_master_key", ""),
            getattr(settings, "secret_master_key_file", ""),
        )
        return cls(
            engine,
            key,
            key_version=int(
                getattr(settings, "secret_master_key_version", 1) or 1
            ),
        )

    def _require_key(self) -> bytes:
        if not self.key:
            raise CredentialError(
                "未配置 AGENT_SECRET_MASTER_KEY；无法保存上游模型 Key"
            )
        return self.key

    @staticmethod
    def _aad(tenant: str, user: str, provider: str) -> bytes:
        return f"{tenant}|{user}|{provider}".encode()

    def get(self, tenant: str, user: str, provider: str) -> str:
        with Session(self.engine) as session:
            row = session.get(
                ControlProviderKey, (str(tenant), str(user), str(provider))
            )
            if row is None or not row.ciphertext:
                return ""
            if not self.key:
                raise CredentialError(
                    "存在已加密的上游 Key，但未配置 AGENT_SECRET_MASTER_KEY"
                )
            try:
                nonce = base64.b64decode(row.nonce)
                ciphertext = base64.b64decode(row.ciphertext)
                plain = AESGCM(self.key).decrypt(
                    nonce,
                    ciphertext,
                    self._aad(tenant, user, provider),
                )
            except Exception as exc:  # noqa: BLE001 - 解密失败 fail closed
                raise CredentialError(
                    "上游 Key 解密失败；请检查主密钥是否变更"
                ) from exc
        return plain.decode("utf-8")

    def set(
        self,
        tenant: str,
        user: str,
        provider: str,
        value: str,
        *,
        created_by: str = "",
    ) -> str:
        key = self._require_key()
        plain = str(value or "")
        if not plain:
            raise CredentialError("API Key 不能为空")
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(
            nonce, plain.encode("utf-8"), self._aad(tenant, user, provider)
        )
        with Session(self.engine) as session, session.begin():
            row = session.get(
                ControlProviderKey, (str(tenant), str(user), str(provider))
            )
            if row is None:
                row = ControlProviderKey(
                    tenant=str(tenant),
                    user=str(user),
                    provider=str(provider),
                )
                session.add(row)
            row.ciphertext = base64.b64encode(ciphertext).decode("ascii")
            row.nonce = base64.b64encode(nonce).decode("ascii")
            row.key_version = self.key_version
            row.last4 = plain[-4:]
            row.enabled = True
            row.created_by = str(created_by or "")
            row.updated_at = _now()
        return plain[-4:]

    def rotate(
        self,
        old_key: bytes,
        new_key: bytes,
        *,
        new_version: int | None = None,
    ) -> int:
        """用新主密钥重加密全部 provider Key；失败整体回滚。"""
        old = bytes(old_key or b"")
        new = bytes(new_key or b"")
        if len(old) != 32 or len(new) != 32:
            raise CredentialError("新旧主密钥都必须是 32 字节")
        version = max(1, int(new_version or self.key_version + 1))
        with Session(self.engine) as session, session.begin():
            rows = session.scalars(select(ControlProviderKey)).all()
            for row in rows:
                if not row.ciphertext:
                    continue
                try:
                    plain = AESGCM(old).decrypt(
                        base64.b64decode(row.nonce),
                        base64.b64decode(row.ciphertext),
                        self._aad(row.tenant, row.user, row.provider),
                    )
                except Exception as exc:  # noqa: BLE001 - 轮换必须整体失败
                    raise CredentialError(
                        "旧主密钥无法解密现有密文，轮换中止"
                    ) from exc
                nonce = secrets.token_bytes(12)
                ciphertext = AESGCM(new).encrypt(
                    nonce,
                    plain,
                    self._aad(row.tenant, row.user, row.provider),
                )
                row.nonce = base64.b64encode(nonce).decode("ascii")
                row.ciphertext = base64.b64encode(ciphertext).decode(
                    "ascii"
                )
                row.key_version = version
                row.updated_at = _now()
        self.key = new
        self.key_version = version
        return len(rows)

    def count(self) -> int:
        with Session(self.engine) as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(ControlProviderKey)
                )
                or 0
            )

    def delete(self, tenant: str, user: str, provider: str) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(
                ControlProviderKey, (str(tenant), str(user), str(provider))
            )
            if row is not None:
                session.delete(row)

    def has(self, tenant: str, user: str, provider: str) -> bool:
        with Session(self.engine) as session:
            row = session.get(
                ControlProviderKey, (str(tenant), str(user), str(provider))
            )
            return bool(row is not None and row.ciphertext and row.enabled)

    def last4(self, tenant: str, user: str, provider: str) -> str:
        with Session(self.engine) as session:
            row = session.get(
                ControlProviderKey, (str(tenant), str(user), str(provider))
            )
            return str(row.last4 or "") if row is not None else ""

    def providers_for(self, tenant: str, user: str) -> list[str]:
        with Session(self.engine) as session:
            return list(
                session.scalars(
                    select(ControlProviderKey.provider).where(
                        ControlProviderKey.tenant == str(tenant),
                        ControlProviderKey.user == str(user),
                        ControlProviderKey.enabled.is_(True),
                    )
                )
            )


def find_master_key_material(
    directory: str | Path,
    key: bytes,
    *,
    key_file: str = "",
) -> list[str]:
    """扫描目录，返回包含主密钥文件/编码或原始字节的路径。"""
    root = Path(directory).expanduser().resolve()
    findings: list[str] = []
    key_path = Path(key_file).expanduser().resolve() if key_file else None
    if key_path is not None and key_path.is_file():
        try:
            if key_path.is_relative_to(root):
                findings.append(str(key_path))
        except ValueError:
            pass
    if not key:
        return findings
    patterns = {
        bytes(key),
        base64.b64encode(key),
        base64.urlsafe_b64encode(key),
        key.hex().encode("ascii"),
    }
    patterns = {item for item in patterns if item}
    overlap = 128
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                tail = b""
                while True:
                    chunk = handle.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    window = tail + chunk
                    if any(pattern in window for pattern in patterns):
                        findings.append(str(path))
                        break
                    tail = window[-overlap:]
        except OSError:
            continue
    return findings


class ScopedSecretStore:
    """ProviderService 使用的适配器：DB 优先，未命中回退 keyring/环境。

    一个 store 实例通常在一次请求内服务多个 provider；首次访问时一次性
    拉取该用户的全部密文，避免 rows()/keys() 的 N+1 查询。
    """

    def __init__(
        self,
        credentials: CredentialStore,
        fallback: Any,
        tenant: str,
        user: str,
    ) -> None:
        self.credentials = credentials
        self.fallback = fallback
        self.tenant = str(tenant)
        self.user = str(user)
        self._rows: dict[str, tuple[str, str, str, bool]] | None = None

    def _load(self) -> dict[str, tuple[str, str, str, bool]]:
        if self._rows is not None:
            return self._rows
        with Session(self.credentials.engine) as session:
            rows = session.execute(
                select(
                    ControlProviderKey.provider,
                    ControlProviderKey.ciphertext,
                    ControlProviderKey.nonce,
                    ControlProviderKey.last4,
                    ControlProviderKey.enabled,
                ).where(
                    ControlProviderKey.tenant == self.tenant,
                    ControlProviderKey.user == self.user,
                )
            ).all()
        self._rows = {
            str(row.provider): (
                str(row.ciphertext or ""),
                str(row.nonce or ""),
                str(row.last4 or ""),
                bool(row.enabled),
            )
            for row in rows
        }
        return self._rows

    def _decrypt(self, name: str, row: tuple[str, str, str, bool]) -> str:
        ciphertext, nonce, _last4, enabled = row
        if not enabled or not ciphertext:
            return ""
        if not self.credentials.key:
            raise CredentialError(
                "存在已加密的上游 Key，但未配置 AGENT_SECRET_MASTER_KEY"
            )
        try:
            plain = AESGCM(self.credentials.key).decrypt(
                base64.b64decode(nonce),
                base64.b64decode(ciphertext),
                self.credentials._aad(self.tenant, self.user, name),
            )
        except Exception as exc:  # noqa: BLE001 - 解密失败 fail closed
            raise CredentialError(
                "上游 Key 解密失败；请检查主密钥是否变更"
            ) from exc
        return plain.decode("utf-8")

    def get(self, name: str) -> str:
        row = self._load().get(str(name))
        if row is not None:
            value = self._decrypt(name, row)
            if value:
                return value
        return self.fallback.get(name)

    def set(self, name: str, value: str) -> None:
        self.credentials.set(
            self.tenant, self.user, name, value, created_by=self.user
        )
        self._rows = None

    def delete(self, name: str) -> None:
        self.credentials.delete(self.tenant, self.user, name)
        self.fallback.delete(name)
        self._rows = None

    def has(self, name: str) -> bool:
        row = self._load().get(str(name))
        if row is not None and row[3] and row[0]:
            return True
        return bool(self.fallback.has(name))

    def last4(self, name: str) -> str:
        row = self._load().get(str(name))
        return row[2] if row is not None else ""
