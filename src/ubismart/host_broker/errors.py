"""A2L 业务错误：由 op/policy 抛出，server 统一转成协议响应。"""

from __future__ import annotations


class BrokerOpError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        data: dict | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.data = dict(data or {})
