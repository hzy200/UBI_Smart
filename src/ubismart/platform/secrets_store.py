"""GUI 注入的 provider 密钥存储。

密钥只进 OS 密钥环（Windows Credential Manager / Linux Secret Service），
不写入 `.env`、不返回给前端、不写日志。未安装 `keyring` 时显式失败，
而不是退化成明文文件。
"""

from __future__ import annotations

_SERVICE = "coding-agent"


class SecretStoreUnavailable(RuntimeError):
    pass


class SecretStore:
    def __init__(self, service: str = _SERVICE) -> None:
        self.service = service

    def _backend(self):
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - 取决于可选依赖
            raise SecretStoreUnavailable(
                '未安装 keyring。请运行：pip install -e ".[secrets]"'
            ) from exc
        return keyring

    def get(self, name: str) -> str:
        try:
            return str(self._backend().get_password(self.service, name) or "")
        except SecretStoreUnavailable:
            return ""
        except Exception:  # noqa: BLE001 - 密钥环不可用时按未配置处理
            return ""

    def set(self, name: str, value: str) -> None:
        backend = self._backend()
        backend.set_password(self.service, name, value)

    def delete(self, name: str) -> None:
        try:
            self._backend().delete_password(self.service, name)
        except Exception:  # noqa: BLE001 - 不存在时视为已删除
            pass

    def has(self, name: str) -> bool:
        return bool(self.get(name))
