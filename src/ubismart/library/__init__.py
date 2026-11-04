"""每用户跨工作区的资料库（Phase 1）。

只存用户主动上传的参考资料，不索引 workspace 文件。正文与索引放在
library_root 下，由 A4 的 `SandboxFs` 读写；A2 只做认证、配额与代理。
"""

from ubismart.library.models import (
    Chunk,
    LibraryError,
    LibraryNotFound,
    LibraryQuotaExceeded,
    LibraryTooLarge,
    SupportedFormatError,
)
from ubismart.library.root import ensure_library_root, resolve_library_root
from ubismart.library.store import LibraryStore

__all__ = [
    "Chunk",
    "LibraryError",
    "LibraryNotFound",
    "LibraryQuotaExceeded",
    "LibraryStore",
    "LibraryTooLarge",
    "SupportedFormatError",
    "ensure_library_root",
    "resolve_library_root",
]
