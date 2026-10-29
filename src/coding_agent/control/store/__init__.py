"""A2 控制面的会话目录存储（Phase B1/B2）。

按聚合根拆成 mixin；`ControlStore` 保持原有的导入路径与组合方式。
"""

from __future__ import annotations

from .audit import AuditMixin
from .base import (
    BaseMixin,
    QuotaExceeded,
    _future,
    _now,
    _tags_from_text,
    _tags_to_text,
    _today,
)
from .notifications import NotificationsMixin
from .search import SearchMixin
from .sessions import SessionsMixin
from .usage import UsageMixin, _quota_row, _usage_row

__all__ = [
    "ControlStore",
    "QuotaExceeded",
    "_future",
    "_now",
    "_quota_row",
    "_tags_from_text",
    "_tags_to_text",
    "_today",
    "_usage_row",
]


class ControlStore(
    BaseMixin,
    SessionsMixin,
    AuditMixin,
    NotificationsMixin,
    SearchMixin,
    UsageMixin,
):
    """控制面存储：连接、会话、审计、通知、检索与配额。"""
