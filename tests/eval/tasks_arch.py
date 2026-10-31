"""架构变更评测集：接口跨多模块传播、调用方全量更新。

这两个任务压的是 RFC-0009 的 ChangeContract/接口优先波次：
- 可见测试在种子状态**通过**，只看绿化测试不会发现需求未满足；
- 判定靠隐藏测试，要求新接口在整条调用链上生效；
- 参考解给出跨 3 个模块的一致改法。
"""

from __future__ import annotations

from harness import CROSS_FILE, DEEP, EvalTask

__all__ = ["TASKS_ARCH"]


def _p(problem: str) -> str:
    return (
        f"项目就在当前工作目录。{problem}\n"
        "请让项目自带的测试保持通过，并按需求完成跨模块改动。"
        "测试文件不要改（判定前会被还原），改动请落在源码上。"
    )


PRICING = '''\
"""价格计算。"""


def compute_total(items):
    return sum(items)
'''

ORDERS = '''\
"""订单。"""

from app.pricing import compute_total


def order_total(items):
    return compute_total(items)
'''

API = '''\
"""对外接口。"""

from app.orders import order_total


def summary(items):
    return order_total(items)
'''

VISIBLE = '''\
import unittest

from app.api import summary


class ApiTests(unittest.TestCase):
    def test_summary(self):
        self.assertEqual(summary([1, 2, 3]), 6)
'''

HIDDEN_RENAME = '''\
import unittest

import app.pricing as pricing

from app.api import summary
from app.pricing import calculate_total


class RenameHidden(unittest.TestCase):
    def test_new_name_works(self):
        self.assertEqual(calculate_total([1, 2]), 3)

    def test_old_name_is_gone(self):
        self.assertFalse(hasattr(pricing, "compute_total"))

    def test_call_chain_uses_new_name(self):
        self.assertEqual(summary([2, 3]), 5)
'''

REF_PRICING = '''\
"""价格计算。"""


def calculate_total(items):
    return sum(items)
'''

REF_ORDERS = '''\
"""订单。"""

from app.pricing import calculate_total


def order_total(items):
    return calculate_total(items)
'''

REF_API = '''\
"""对外接口。"""

from app.orders import order_total


def summary(items):
    return order_total(items)
'''

HIDDEN_CURRENCY = '''\
import unittest

from app.api import summary_currency


class CurrencyHidden(unittest.TestCase):
    def test_currency_propagates(self):
        self.assertEqual(
            summary_currency([1, 2, 3], "USD"),
            {"total": 6, "currency": "USD"},
        )

    def test_default_currency(self):
        self.assertEqual(
            summary_currency([2, 2]),
            {"total": 4, "currency": "CNY"},
        )
'''

REF2_PRICING = '''\
"""价格计算。"""


def compute_total(items):
    return sum(items)


def compute_total_with_currency(items, currency="CNY"):
    return {"total": compute_total(items), "currency": currency}
'''

REF2_ORDERS = '''\
"""订单。"""

from app.pricing import compute_total, compute_total_with_currency


def order_total(items):
    return compute_total(items)


def order_total_with_currency(items, currency="CNY"):
    return compute_total_with_currency(items, currency)
'''

REF2_API = '''\
"""对外接口。"""

from app.orders import order_total, order_total_with_currency


def summary(items):
    return order_total(items)


def summary_currency(items, currency="CNY"):
    return order_total_with_currency(items, currency)
'''

TASKS_ARCH: tuple[EvalTask, ...] = (
    EvalTask(
        id="arch/interface_rename",
        category=CROSS_FILE,
        tier=DEEP,
        prompt=_p(
            "把 `app.pricing.compute_total` 重命名为 `calculate_total`。"
            "所有调用方（orders、api）都必须同步更新，旧名字不能再存在。"
        ),
        sources={
            "app/__init__.py": "",
            "app/pricing.py": PRICING,
            "app/orders.py": ORDERS,
            "app/api.py": API,
            "tests/__init__.py": "",
        },
        tests={"tests/test_api.py": VISIBLE},
        hidden_tests={"tests/test_hidden_rename.py": HIDDEN_RENAME},
        reference={
            "app/pricing.py": REF_PRICING,
            "app/orders.py": REF_ORDERS,
            "app/api.py": REF_API,
        },
    ),
    EvalTask(
        id="arch/currency_propagation",
        category=CROSS_FILE,
        tier=DEEP,
        prompt=_p(
            "新增 `app.api.summary_currency(items, currency='CNY')`，"
            "返回 `{'total': ..., 'currency': ...}`；"
            "currency 必须从 api → orders → pricing 完整传递，默认 CNY。"
        ),
        sources={
            "app/__init__.py": "",
            "app/pricing.py": PRICING,
            "app/orders.py": ORDERS,
            "app/api.py": API,
            "tests/__init__.py": "",
        },
        tests={"tests/test_api.py": VISIBLE},
        hidden_tests={"tests/test_hidden_currency.py": HIDDEN_CURRENCY},
        reference={
            "app/pricing.py": REF2_PRICING,
            "app/orders.py": REF2_ORDERS,
            "app/api.py": REF2_API,
        },
    ),
)
