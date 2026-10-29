"""兼容导入：实现已移到 `coding_agent.platform.secrets_store`。"""

from __future__ import annotations

from coding_agent.platform.secrets_store import SecretStore, SecretStoreUnavailable

__all__ = ["SecretStore", "SecretStoreUnavailable"]
