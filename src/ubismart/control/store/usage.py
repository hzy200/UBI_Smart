"""控制面用量与配额（记账 / 预留 / 报表）。

拆成三个 mixin 后仍以 `UsageMixin` 组合，`ControlStore` 的 MRO 不变。
"""

from __future__ import annotations

from .usage_common import _quota_row
from .usage_record import UsageRecordMixin
from .usage_report import UsageReportMixin, _usage_row
from .usage_reserve import UsageReserveMixin

__all__ = ["UsageMixin", "_quota_row", "_usage_row"]


class UsageMixin(UsageRecordMixin, UsageReserveMixin, UsageReportMixin):
    """用量记账、配额预留与报表。"""
