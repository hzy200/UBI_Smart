"""成本货币换算。

上游 provider 的价格与运行时用量始终按 USD 记账；人民币只用于配置、额度
与展示。汇率由 `AGENT_USD_TO_CNY` 固定配置，不依赖网络行情，保证配额判断
可复现。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

SUPPORTED_CURRENCIES = {"CNY", "USD"}
_MONEY_QUANT = Decimal("0.0001")


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")


@dataclass(frozen=True)
class CurrencyConfig:
    code: str = "CNY"
    usd_to_cny: Decimal = Decimal("7.2")

    @classmethod
    def from_settings(cls, settings: Any) -> CurrencyConfig:
        code = str(getattr(settings, "cost_currency", "CNY") or "CNY").upper()
        if code not in SUPPORTED_CURRENCIES:
            raise ValueError(f"不支持的成本货币：{code}")
        rate = _decimal(getattr(settings, "usd_to_cny", 7.2))
        if rate <= 0:
            raise ValueError("AGENT_USD_TO_CNY 必须大于 0")
        return cls(code=code, usd_to_cny=rate)

    @property
    def symbol(self) -> str:
        return "¥" if self.code == "CNY" else "$"

    @property
    def rate(self) -> Decimal:
        return Decimal("1") if self.code == "USD" else self.usd_to_cny

    def from_usd(self, value: Any) -> Decimal:
        return (_decimal(value) * self.rate).quantize(
            _MONEY_QUANT, rounding=ROUND_HALF_UP
        )

    def to_usd(self, value: Any) -> Decimal:
        return (_decimal(value) / self.rate).quantize(
            Decimal("0.0000001"), rounding=ROUND_HALF_UP
        )

    def format(self, value: Any, *, decimals: int = 4) -> str:
        amount = self.from_usd(value)
        text = f"{amount:.{decimals}f}"
        return f"{self.symbol}{text}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "currency": self.code,
            "usdToCny": float(self.usd_to_cny),
        }
