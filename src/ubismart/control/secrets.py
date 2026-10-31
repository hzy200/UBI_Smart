"""A2 的 SecretProvider（Phase B1）。

两档：

- 桌面：OS 密钥环（Windows Credential Manager / Linux Secret Service）；
- 服务器：systemd-creds（``$CREDENTIALS_DIRECTORY`` 下的只读文件），
  Vault/KMS 通过同一个 Protocol 接入。

WSL 通常没有 Secret Service daemon；因此 A3 不应自己持久化密钥，
由 A2 在运行期短期注入内存。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from ubismart.config import Settings
from ubismart.platform.secrets_store import SecretStore, SecretStoreUnavailable


class SecretProvider(Protocol):
    def get(self, name: str) -> str: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...
    def has(self, name: str) -> bool: ...


class KeyringSecretProvider:
    """桌面默认：复用现有 OS 密钥环实现。"""

    def __init__(self, service: str = "coding-agent") -> None:
        self._store = SecretStore(service)

    def get(self, name: str) -> str:
        return self._store.get(name)

    def set(self, name: str, value: str) -> None:
        self._store.set(name, value)

    def delete(self, name: str) -> None:
        self._store.delete(name)

    def has(self, name: str) -> bool:
        return self._store.has(name)


class SystemdCredsSecretProvider:
    """服务器只读凭据：systemd 把密钥放在 CREDENTIALS_DIRECTORY。

    systemd-creds 文件是不可变注入物；运行期写/删应走 Vault/KMS 或部署系统，
    这里显式失败，避免悄悄退回明文文件。
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        raw = directory or os.environ.get("CREDENTIALS_DIRECTORY", "")
        self.directory = Path(str(raw)) if raw else None

    def _path(self, name: str) -> Path:
        if self.directory is None:
            raise SecretStoreUnavailable(
                "未设置 CREDENTIALS_DIRECTORY；systemd-creds 不可用"
            )
        safe = "".join(
            ch for ch in str(name) if ch.isalnum() or ch in "_.-"
        )[:64]
        if not safe:
            raise SecretStoreUnavailable("非法 provider 名")
        return self.directory / safe

    def get(self, name: str) -> str:
        try:
            return self._path(name).read_text(encoding="utf-8").strip()
        except (OSError, SecretStoreUnavailable):
            return ""

    def set(self, name: str, value: str) -> None:
        raise SecretStoreUnavailable(
            "systemd-creds 是只读注入；请通过部署系统更新凭据"
        )

    def delete(self, name: str) -> None:
        raise SecretStoreUnavailable(
            "systemd-creds 是只读注入；请通过部署系统移除凭据"
        )

    def has(self, name: str) -> bool:
        return bool(self.get(name))


def build_secret_provider(
    settings: Settings, *, service: str = "coding-agent"
) -> SecretProvider:
    choice = str(settings.secret_provider or "auto").strip().lower()
    if choice in {"systemd", "systemd-creds"}:
        return SystemdCredsSecretProvider()
    if choice == "keyring":
        return KeyringSecretProvider(service)
    # auto：有 systemd 注入目录时优先只读凭据，否则桌面 keyring。
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        return SystemdCredsSecretProvider()
    return KeyringSecretProvider(service)
