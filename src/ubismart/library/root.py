"""资料库根目录解析。

资料库是**每用户跨工作区**的，所以默认放在沙箱家目录，而不是 workspace 的
`.agent/` 下。显式配置优先；配置成 workspace 内部时直接拒绝，避免退化成
“每个工作区一份”。
"""

from __future__ import annotations

import posixpath
from typing import Any

from ubismart.library.models import LibraryError
from ubismart.sandbox.pathguard import is_within, normalize_root


def resolve_library_root(
    settings: Any,
    sandbox: Any,
    workspace: str = "",
) -> str:
    configured = str(getattr(settings, "library_root", "") or "").strip()
    if configured:
        root = normalize_root(configured)
    else:
        home = sandbox.home() if callable(getattr(sandbox, "home", None)) else ""
        if not home:
            raise LibraryError("无法解析沙箱家目录，资料库不可用")
        root = posixpath.join(str(home), ".agent-library")
    if workspace and is_within(root, workspace):
        raise LibraryError(
            "资料库根目录不能位于当前工作区内；"
            "资料库按用户跨工作区保存，请配置 AGENT_LIBRARY_ROOT"
        )
    return root


def ensure_library_root(
    settings: Any,
    sandbox: Any,
    workspace: str = "",
) -> str:
    """解析并创建资料库根目录。

    `SandboxFs.mkdir` 只接受根内路径，无法创建根本身；这里用沙箱的宿主
    管理动作 `ensure_directory` 创建，再交给 `SandboxFs` 做后续校验。
    """
    root = resolve_library_root(settings, sandbox, workspace)
    create = getattr(sandbox, "ensure_directory", None)
    if callable(create):
        try:
            create(root)
        except Exception as exc:  # noqa: BLE001
            raise LibraryError(f"无法创建资料库目录：{exc}") from exc
    return root
