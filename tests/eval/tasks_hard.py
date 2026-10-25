"""hard 套件：用于恢复评测区分度的 Python 硬任务。

现有 `default` / `full` 套件对当前模型已饱和（100%），单次运行无法区分改动
优劣。这里补 14 个更接近真实工程难度的任务，覆盖五类：

- 并发/竞态：锁、异步限流、指数退避、令牌桶；
- 需求未覆盖：可见测试全绿，隐藏测试才检查分页游标、幂等、时区；
- 多层贯通：模型/服务/报表的币种字段、配置优先级、插件注册表；
- 排障：缓存防御性拷贝、批量导入的原子性；
- 性能：批量读取（N+1）、LRU 淘汰。

每个任务都提供参考解；`runner.py --suite hard --verify-seeds` 会验证
「种子必失败、参考解必通过」。判定仍用 stdlib unittest，不引入第三方依赖。
"""

from __future__ import annotations

from harness import (
    CONCURRENCY,
    CROSS_FILE,
    DEBUG,
    DEEP,
    PERFORMANCE,
    SPEC,
    EvalTask,
)


def _prompt(text: str) -> str:
    return (
        f"项目就在当前工作目录。{text}\n"
        "请让项目自带的测试全部通过。测试文件不要改"
        "（判定前会被还原），改动请落在源码上。"
    )


def _task(
    task_id: str,
    category: str,
    prompt: str,
    sources: dict[str, str],
    tests: dict[str, str],
    reference: dict[str, str],
    hidden_tests: dict[str, str] | None = None,
) -> EvalTask:
    return EvalTask(
        id=task_id,
        category=category,
        tier=DEEP,
        prompt=_prompt(prompt),
        sources=sources,
        tests=tests,
        hidden_tests=hidden_tests or {},
        reference=reference,
        timeout=300,
        agent_timeout=300,
    )


TASKS_HARD: list[EvalTask] = [
    # ------------------------------------------------------------------
    # 并发 / 竞态
    # ------------------------------------------------------------------
    _task(
        "hard/concurrency/exactly_once_registry",
        CONCURRENCY,
        "`Registry.get_or_create(key, factory)` 必须保证：同一个 key 的 factory "
        "在并发下也只执行一次；所有调用者拿到同一个结果。",
        {
            "registry.py": (
                "import time\n\n\n"
                "class Registry:\n"
                "    def __init__(self):\n"
                "        self._items = {}\n"
                "        self._loads = 0\n\n"
                "    def get_or_create(self, key, factory):\n"
                "        if key in self._items:\n"
                "            return self._items[key]\n"
                "        self._loads += 1\n"
                "        value = factory(key)\n"
                "        time.sleep(0.01)\n"
                "        self._items[key] = value\n"
                "        return value\n"
            )
        },
        {
            "test_registry.py": '''\
import threading
import time
import unittest

from registry import Registry


class RegistryTest(unittest.TestCase):
    def test_factory_runs_once_under_contention(self):
        registry = Registry()
        barrier = threading.Barrier(8)
        first_entered = threading.Event()
        calls = []
        results = []

        def factory(key):
            calls.append(key)
            if len(calls) == 1:
                first_entered.set()
            time.sleep(0.05)
            return key.upper()

        def worker():
            barrier.wait()
            results.append(registry.get_or_create("k", factory))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        first_entered.wait(1)
        # 给未加锁的实现留出所有线程都进入 factory 的窗口。
        time.sleep(0.05)
        for thread in threads:
            thread.join()

        self.assertEqual(results, ["K"] * 8)
        self.assertEqual(len(calls), 1)
'''
        },
        {
            "registry.py": (
                "import threading\n\n\n"
                "class Registry:\n"
                "    def __init__(self):\n"
                "        self._items = {}\n"
                "        self._loads = 0\n"
                "        self._lock = threading.Lock()\n\n"
                "    def get_or_create(self, key, factory):\n"
                "        with self._lock:\n"
                "            if key in self._items:\n"
                "                return self._items[key]\n"
                "            self._loads += 1\n"
                "            value = factory(key)\n"
                "            self._items[key] = value\n"
                "            return value\n"
            )
        },
    ),
    _task(
        "hard/concurrency/async_bounded_pool",
        CONCURRENCY,
        "`run_all(jobs, limit)` 并发执行所有协程，但在任一时刻最多只有 "
        "`limit` 个 job 在运行；limit <= 0 抛 ValueError。",
        {
            "pool.py": (
                "import asyncio\n\n\n"
                "async def run_all(jobs, limit):\n"
                "    return await asyncio.gather(*(job() for job in jobs))\n"
            )
        },
        {
            "test_pool.py": '''\
import asyncio
import unittest

from pool import run_all


class PoolTest(unittest.TestCase):
    def test_limits_concurrency(self):
        state = {"active": 0, "peak": 0}

        async def job():
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.01)
            state["active"] -= 1
            return True

        results = asyncio.run(run_all([job for _ in range(12)], 3))

        self.assertEqual(results, [True] * 12)
        self.assertLessEqual(state["peak"], 3)
        self.assertGreaterEqual(state["peak"], 2)

    def test_rejects_non_positive_limit(self):
        async def job():
            return 1

        with self.assertRaises(ValueError):
            asyncio.run(run_all([job], 0))
'''
        },
        {
            "pool.py": (
                "import asyncio\n\n\n"
                "async def run_all(jobs, limit):\n"
                "    if limit <= 0:\n"
                "        raise ValueError(\"limit must be positive\")\n"
                "    semaphore = asyncio.Semaphore(limit)\n\n"
                "    async def run(job):\n"
                "        async with semaphore:\n"
                "            return await job()\n\n"
                "    return await asyncio.gather(*(run(job) for job in jobs))\n"
            )
        },
    ),
    _task(
        "hard/concurrency/retry_backoff",
        CONCURRENCY,
        "`retry` 在失败后必须按指数退避（base_delay, 2×, 4×…，不超过 "
        "max_delay）再重试；`sleep` 是注入的休眠函数，便于测试。",
        {
            "retry.py": (
                "def retry(func, attempts, *, sleep, base_delay=0.1, max_delay=1.0):\n"
                "    last = None\n"
                "    for _ in range(attempts):\n"
                "        try:\n"
                "            return func()\n"
                "        except Exception as exc:\n"
                "            last = exc\n"
                "    raise last\n"
            )
        },
        {
            "test_retry.py": '''\
import unittest

from retry import retry


class RetryTest(unittest.TestCase):
    def test_backoff_between_attempts(self):
        delays = []
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            return "ok"

        self.assertEqual(retry(flaky, 5, sleep=delays.append), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [0.1, 0.2])

    def test_caps_delay(self):
        delays = []

        def always_fails():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            retry(
                always_fails,
                5,
                sleep=delays.append,
                base_delay=1.0,
                max_delay=1.5,
            )
        self.assertEqual(delays, [1.0, 1.5, 1.5, 1.5])
'''
        },
        {
            "retry.py": (
                "def retry(func, attempts, *, sleep, base_delay=0.1, max_delay=1.0):\n"
                "    if attempts <= 0:\n"
                "        raise ValueError(\"attempts must be positive\")\n"
                "    last = None\n"
                "    for index in range(attempts):\n"
                "        try:\n"
                "            return func()\n"
                "        except Exception as exc:\n"
                "            last = exc\n"
                "            if index + 1 < attempts:\n"
                "                sleep(min(base_delay * (2 ** index), max_delay))\n"
                "    raise last\n"
            )
        },
    ),
    _task(
        "hard/concurrency/token_bucket",
        CONCURRENCY,
        "实现令牌桶：初始有 capacity 个令牌，按 refill_per_sec 线性补充且不超过 "
        "容量；`allow(now)` 有令牌时消费一个并返回 True。",
        {
            "bucket.py": (
                "class TokenBucket:\n"
                "    def __init__(self, capacity, refill_per_sec):\n"
                "        self.capacity = capacity\n"
                "        self.refill_per_sec = refill_per_sec\n\n"
                "    def allow(self, now):\n"
                "        return True\n"
            )
        },
        {
            "test_bucket.py": '''\
import unittest

from bucket import TokenBucket


class BucketTest(unittest.TestCase):
    def test_consumes_and_refills(self):
        bucket = TokenBucket(2, 1.0)
        self.assertTrue(bucket.allow(0.0))
        self.assertTrue(bucket.allow(0.0))
        self.assertFalse(bucket.allow(0.0))
        self.assertTrue(bucket.allow(1.0))
        self.assertFalse(bucket.allow(1.0))

    def test_never_exceeds_capacity(self):
        bucket = TokenBucket(1, 10.0)
        self.assertTrue(bucket.allow(0.0))
        self.assertTrue(bucket.allow(100.0))
        self.assertFalse(bucket.allow(100.0))
'''
        },
        {
            "bucket.py": (
                "class TokenBucket:\n"
                "    def __init__(self, capacity, refill_per_sec):\n"
                "        self.capacity = float(capacity)\n"
                "        self.refill_per_sec = float(refill_per_sec)\n"
                "        self._tokens = float(capacity)\n"
                "        self._last = 0.0\n\n"
                "    def allow(self, now):\n"
                "        elapsed = max(0.0, float(now) - self._last)\n"
                "        self._tokens = min(\n"
                "            self.capacity,\n"
                "            self._tokens + elapsed * self.refill_per_sec,\n"
                "        )\n"
                "        self._last = float(now)\n"
                "        if self._tokens + 1e-9 < 1.0:\n"
                "            return False\n"
                "        self._tokens -= 1.0\n"
                "        return True\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    # 需求未覆盖（可见测试全绿，隐藏测试才检查）
    # ------------------------------------------------------------------
    _task(
        "hard/spec/cursor_pagination",
        SPEC,
        "分页必须支持游标：按 (created_at, id) 倒序；cursor 是上一页最后一条的 "
        "id，返回它之后的数据；未知 cursor 抛 ValueError；limit 必须为正。",
        {
            "pagination.py": (
                "def page(items, cursor=None, limit=2):\n"
                "    return list(items)[:limit]\n"
            )
        },
        {
            "test_pagination.py": '''\
import unittest

from pagination import page


class PaginationTest(unittest.TestCase):
    def test_first_page_without_cursor(self):
        items = [
            {"id": "a", "created_at": 3},
            {"id": "b", "created_at": 2},
            {"id": "c", "created_at": 1},
        ]
        self.assertEqual(
            [item["id"] for item in page(items, limit=2)], ["a", "b"]
        )
'''
        },
        {
            "pagination.py": (
                "def page(items, cursor=None, limit=2):\n"
                "    if limit <= 0:\n"
                "        raise ValueError(\"limit must be positive\")\n"
                "    ordered = sorted(\n"
                "        items,\n"
                "        key=lambda item: (item[\"created_at\"], item[\"id\"]),\n"
                "        reverse=True,\n"
                "    )\n"
                "    if cursor is not None:\n"
                "        index = next(\n"
                "            (\n"
                "                position\n"
                "                for position, item in enumerate(ordered)\n"
                "                if item[\"id\"] == cursor\n"
                "            ),\n"
                "            None,\n"
                "        )\n"
                "        if index is None:\n"
                "            raise ValueError(\"unknown cursor\")\n"
                "        ordered = ordered[index + 1:]\n"
                "    return ordered[:limit]\n"
            )
        },
        {
            "test_pagination_hidden.py": '''\
import unittest

from pagination import page


class PaginationHiddenTest(unittest.TestCase):
    def test_cursor_returns_the_next_page(self):
        items = [
            {"id": "a", "created_at": 3},
            {"id": "b", "created_at": 2},
            {"id": "c", "created_at": 1},
        ]
        self.assertEqual(
            [item["id"] for item in page(items, cursor="a", limit=2)],
            ["b", "c"],
        )

    def test_ordering_is_stable_for_equal_timestamps(self):
        items = [
            {"id": "b", "created_at": 2},
            {"id": "a", "created_at": 2},
            {"id": "c", "created_at": 1},
        ]
        self.assertEqual(
            [item["id"] for item in page(items, limit=2)], ["b", "a"]
        )
        self.assertEqual(
            [item["id"] for item in page(items, cursor="b", limit=2)],
            ["a", "c"],
        )

    def test_unknown_cursor_and_bad_limit(self):
        items = [{"id": "a", "created_at": 1}]
        with self.assertRaises(ValueError):
            page(items, cursor="missing")
        with self.assertRaises(ValueError):
            page(items, limit=0)
'''
        },
    ),
    _task(
        "hard/spec/idempotent_create",
        SPEC,
        "`Store.create(request_id, value)` 必须按 request_id 幂等：重复请求返回"
        "已存在的行，不能覆盖或追加；request_id 为空抛 ValueError。",
        {
            "store.py": (
                "class Store:\n"
                "    def __init__(self):\n"
                "        self.rows = []\n\n"
                "    def create(self, request_id, value):\n"
                "        row = {\"request_id\": request_id, \"value\": value}\n"
                "        self.rows.append(row)\n"
                "        return row\n"
            )
        },
        {
            "test_store.py": '''\
import unittest

from store import Store


class StoreTest(unittest.TestCase):
    def test_creates_a_row(self):
        store = Store()
        row = store.create("r1", "first")
        self.assertEqual(row, {"request_id": "r1", "value": "first"})
        self.assertEqual(len(store.rows), 1)
'''
        },
        {
            "store.py": (
                "class Store:\n"
                "    def __init__(self):\n"
                "        self.rows = []\n"
                "        self._by_request = {}\n\n"
                "    def create(self, request_id, value):\n"
                "        if not str(request_id or \"\").strip():\n"
                "            raise ValueError(\"request_id is required\")\n"
                "        if request_id in self._by_request:\n"
                "            return self._by_request[request_id]\n"
                "        row = {\"request_id\": request_id, \"value\": value}\n"
                "        self.rows.append(row)\n"
                "        self._by_request[request_id] = row\n"
                "        return row\n"
            )
        },
        {
            "test_store_hidden.py": '''\
import unittest

from store import Store


class StoreHiddenTest(unittest.TestCase):
    def test_same_request_returns_the_same_row(self):
        store = Store()
        first = store.create("r1", "first")
        second = store.create("r1", "second")
        self.assertEqual(first, second)
        self.assertEqual(first["value"], "first")
        self.assertEqual(len(store.rows), 1)

    def test_different_requests_create_separate_rows(self):
        store = Store()
        store.create("r1", "a")
        store.create("r2", "b")
        self.assertEqual(len(store.rows), 2)

    def test_empty_request_id_is_rejected(self):
        store = Store()
        with self.assertRaises(ValueError):
            store.create("", "x")
        self.assertEqual(store.rows, [])
'''
        },
    ),
    _task(
        "hard/spec/dst_local_date",
        SPEC,
        "`local_date(iso_utc, tz)` 返回该时刻在指定时区的本地日期；必须正确处理 "
        "夏令时；没有时区信息的输入抛 ValueError。",
        {
            "dates.py": (
                "from datetime import datetime\n\n\n"
                "def local_date(iso_utc, tz):\n"
                "    text = iso_utc.replace(\"Z\", \"+00:00\")\n"
                "    return datetime.fromisoformat(text).date().isoformat()\n"
            )
        },
        {
            "test_dates.py": '''\
import unittest

from dates import local_date


class DatesTest(unittest.TestCase):
    def test_utc_midday_keeps_the_same_date(self):
        self.assertEqual(
            local_date("2024-01-01T12:00:00Z", "America/New_York"),
            "2024-01-01",
        )
'''
        },
        {
            "dates.py": (
                "from datetime import datetime\n"
                "from zoneinfo import ZoneInfo\n\n\n"
                "def local_date(iso_utc, tz):\n"
                "    text = iso_utc.replace(\"Z\", \"+00:00\")\n"
                "    moment = datetime.fromisoformat(text)\n"
                "    if moment.tzinfo is None:\n"
                "        raise ValueError(\"timestamp must include a timezone\")\n"
                "    return moment.astimezone(ZoneInfo(tz)).date().isoformat()\n"
            )
        },
        {
            "test_dates_hidden.py": '''\
import unittest

from dates import local_date


class DatesHiddenTest(unittest.TestCase):
    def test_previous_local_date(self):
        self.assertEqual(
            local_date("2024-01-01T02:00:00Z", "America/New_York"),
            "2023-12-31",
        )

    def test_dst_boundary(self):
        self.assertEqual(
            local_date("2024-03-10T06:30:00Z", "America/New_York"),
            "2024-03-10",
        )
        self.assertEqual(
            local_date("2024-03-10T07:30:00Z", "America/New_York"),
            "2024-03-10",
        )

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            local_date("2024-01-01T12:00:00", "UTC")
'''
        },
    ),
    # ------------------------------------------------------------------
    # 多层贯通
    # ------------------------------------------------------------------
    _task(
        "hard/multilayer/currency_flow",
        CROSS_FILE,
        "订单需要支持币种：`Service.place` 接受 currency（默认 USD），Order 保存"
        "该字段；`report.revenue_by_currency(repo)` 返回 "
        "`{currency: total_cents}`；空币种抛 ValueError。",
        {
            "models.py": (
                "from dataclasses import dataclass\n\n\n"
                "@dataclass\n"
                "class Order:\n"
                "    id: str\n"
                "    amount_cents: int\n"
            ),
            "repo.py": (
                "class Repo:\n"
                "    def __init__(self):\n"
                "        self.orders = {}\n\n"
                "    def save(self, order):\n"
                "        self.orders[order.id] = order\n\n"
                "    def all(self):\n"
                "        return list(self.orders.values())\n"
            ),
            "service.py": (
                "from models import Order\n\n\n"
                "class Service:\n"
                "    def __init__(self, repo):\n"
                "        self.repo = repo\n\n"
                "    def place(self, order_id, amount_cents):\n"
                "        order = Order(id=order_id, amount_cents=amount_cents)\n"
                "        self.repo.save(order)\n"
                "        return order\n"
            ),
            "report.py": (
                "def revenue(repo):\n"
                "    return sum(order.amount_cents for order in repo.all())\n"
            ),
        },
        {
            "test_flow.py": '''\
import unittest

from repo import Repo
from report import revenue
from service import Service


class FlowTest(unittest.TestCase):
    def test_place_and_total(self):
        repo = Repo()
        order = Service(repo).place("o1", 1500)
        self.assertEqual(order.amount_cents, 1500)
        self.assertEqual(revenue(repo), 1500)
'''
        },
        {
            "models.py": (
                "from dataclasses import dataclass\n\n\n"
                "@dataclass\n"
                "class Order:\n"
                "    id: str\n"
                "    amount_cents: int\n"
                "    currency: str = \"USD\"\n"
            ),
            "repo.py": (
                "class Repo:\n"
                "    def __init__(self):\n"
                "        self.orders = {}\n\n"
                "    def save(self, order):\n"
                "        self.orders[order.id] = order\n\n"
                "    def all(self):\n"
                "        return list(self.orders.values())\n"
            ),
            "service.py": (
                "from models import Order\n\n\n"
                "class Service:\n"
                "    def __init__(self, repo):\n"
                "        self.repo = repo\n\n"
                "    def place(self, order_id, amount_cents, currency=\"USD\"):\n"
                "        if not str(currency or \"\").strip():\n"
                "            raise ValueError(\"currency is required\")\n"
                "        order = Order(\n"
                "            id=order_id,\n"
                "            amount_cents=amount_cents,\n"
                "            currency=currency,\n"
                "        )\n"
                "        self.repo.save(order)\n"
                "        return order\n"
            ),
            "report.py": (
                "def revenue(repo):\n"
                "    return sum(order.amount_cents for order in repo.all())\n\n\n"
                "def revenue_by_currency(repo):\n"
                "    totals = {}\n"
                "    for order in repo.all():\n"
                "        totals[order.currency] = (\n"
                "            totals.get(order.currency, 0) + order.amount_cents\n"
                "        )\n"
                "    return totals\n"
            ),
        },
        {
            "test_flow_hidden.py": '''\
import unittest

from repo import Repo
from report import revenue_by_currency
from service import Service


class FlowHiddenTest(unittest.TestCase):
    def test_currency_is_persisted_and_grouped(self):
        repo = Repo()
        service = Service(repo)
        eur = service.place("o1", 1500, currency="EUR")
        service.place("o2", 500, currency="USD")
        self.assertEqual(eur.currency, "EUR")
        self.assertEqual(
            revenue_by_currency(repo), {"EUR": 1500, "USD": 500}
        )

    def test_empty_currency_is_rejected(self):
        service = Service(Repo())
        with self.assertRaises(ValueError):
            service.place("o3", 100, currency="")
'''
        },
    ),
    _task(
        "hard/multilayer/config_precedence",
        CROSS_FILE,
        "`resolve` 按优先级合并配置：defaults < file < env < cli；空字符串与 None "
        "视为未设置，不能覆盖低优先级的值。",
        {
            "config.py": (
                "def resolve(defaults, file_values, env_values, cli_values):\n"
                "    result = dict(defaults)\n"
                "    result.update(cli_values)\n"
                "    return result\n"
            )
        },
        {
            "test_config.py": '''\
import unittest

from config import resolve


class ConfigTest(unittest.TestCase):
    def test_cli_wins_over_default(self):
        self.assertEqual(
            resolve({"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}), {"a": 4}
        )
'''
        },
        {
            "config.py": (
                "def resolve(defaults, file_values, env_values, cli_values):\n"
                "    result = dict(defaults)\n"
                "    for layer in (file_values, env_values, cli_values):\n"
                "        for key, value in layer.items():\n"
                "            if value is None or value == \"\":\n"
                "                continue\n"
                "            result[key] = value\n"
                "    return result\n"
            )
        },
        {
            "test_config_hidden.py": '''\
import unittest

from config import resolve


class ConfigHiddenTest(unittest.TestCase):
    def test_layer_precedence(self):
        base = {"a": 1, "b": 2}
        self.assertEqual(
            resolve(base, {"a": 10}, {"a": 20}, {"a": 30}),
            {"a": 30, "b": 2},
        )
        self.assertEqual(
            resolve(base, {"a": 10}, {"a": 20}, {}),
            {"a": 20, "b": 2},
        )
        self.assertEqual(
            resolve(base, {"a": 10}, {}, {}),
            {"a": 10, "b": 2},
        )

    def test_empty_values_do_not_override(self):
        self.assertEqual(
            resolve({"a": 1}, {"a": ""}, {"a": None}, {}), {"a": 1}
        )
'''
        },
    ),
    _task(
        "hard/multilayer/plugin_priority",
        CROSS_FILE,
        "插件注册表：同名插件按 priority 取最高者（同优先级后注册的胜出），低优先级"
        "不能覆盖高优先级；`ordered()` 按优先级降序、名称升序；空名称抛 ValueError。",
        {
            "plugins.py": (
                "class Registry:\n"
                "    def __init__(self):\n"
                "        self._items = {}\n\n"
                "    def register(self, name, handler, priority=0):\n"
                "        self._items[name] = handler\n\n"
                "    def resolve(self, name):\n"
                "        return self._items.get(name)\n"
            )
        },
        {
            "test_plugins.py": '''\
import unittest

from plugins import Registry


class PluginsTest(unittest.TestCase):
    def test_register_and_resolve(self):
        registry = Registry()
        registry.register("a", lambda: 1)
        self.assertEqual(registry.resolve("a")(), 1)
        self.assertIsNone(registry.resolve("missing"))
'''
        },
        {
            "plugins.py": (
                "class Registry:\n"
                "    def __init__(self):\n"
                "        self._by_name = {}\n\n"
                "    def register(self, name, handler, priority=0):\n"
                "        if not str(name or \"\").strip():\n"
                "            raise ValueError(\"name is required\")\n"
                "        existing = self._by_name.get(name)\n"
                "        if existing is None or priority >= existing[0]:\n"
                "            self._by_name[name] = (priority, handler)\n\n"
                "    def resolve(self, name):\n"
                "        item = self._by_name.get(name)\n"
                "        return item[1] if item else None\n\n"
                "    def ordered(self):\n"
                "        return [\n"
                "            name\n"
                "            for name, _item in sorted(\n"
                "                self._by_name.items(),\n"
                "                key=lambda item: (-item[1][0], item[0]),\n"
                "            )\n"
                "        ]\n"
            )
        },
        {
            "test_plugins_hidden.py": '''\
import unittest

from plugins import Registry


class PluginsHiddenTest(unittest.TestCase):
    def test_priority_wins(self):
        registry = Registry()
        registry.register("a", lambda: "low", priority=1)
        registry.register("a", lambda: "high", priority=5)
        self.assertEqual(registry.resolve("a")(), "high")
        registry.register("a", lambda: "lower", priority=0)
        self.assertEqual(registry.resolve("a")(), "high")

    def test_ordered(self):
        registry = Registry()
        registry.register("b", lambda: "b", priority=3)
        registry.register("a", lambda: "a", priority=5)
        registry.register("c", lambda: "c", priority=3)
        self.assertEqual(registry.ordered(), ["a", "b", "c"])

    def test_empty_name_is_rejected(self):
        registry = Registry()
        with self.assertRaises(ValueError):
            registry.register("", lambda: 1)
'''
        },
    ),
    # ------------------------------------------------------------------
    # 排障
    # ------------------------------------------------------------------
    _task(
        "hard/debug/cache_isolation",
        DEBUG,
        "`UserCache` 的 get/put 必须做防御性拷贝：调用方修改传入的 payload 或"
        "取出的结果，都不能改变缓存中的内容。",
        {
            "user_cache.py": (
                "class UserCache:\n"
                "    def __init__(self):\n"
                "        self._data = {}\n\n"
                "    def put(self, user_id, payload):\n"
                "        self._data[user_id] = payload\n\n"
                "    def get(self, user_id):\n"
                "        return self._data[user_id]\n"
            )
        },
        {
            "test_user_cache.py": '''\
import unittest

from user_cache import UserCache


class UserCacheTest(unittest.TestCase):
    def test_put_and_get(self):
        cache = UserCache()
        cache.put("u1", {"name": "a"})
        self.assertEqual(cache.get("u1"), {"name": "a"})

    def test_get_result_is_detached(self):
        cache = UserCache()
        cache.put("u1", {"name": "a", "roles": ["admin"]})
        result = cache.get("u1")
        result["name"] = "b"
        result["roles"].append("root")
        self.assertEqual(
            cache.get("u1"), {"name": "a", "roles": ["admin"]}
        )
'''
        },
        {
            "user_cache.py": (
                "import copy\n\n\n"
                "class UserCache:\n"
                "    def __init__(self):\n"
                "        self._data = {}\n\n"
                "    def put(self, user_id, payload):\n"
                "        self._data[user_id] = copy.deepcopy(payload)\n\n"
                "    def get(self, user_id):\n"
                "        return copy.deepcopy(self._data[user_id])\n"
            )
        },
        {
            "test_user_cache_hidden.py": '''\
import unittest

from user_cache import UserCache


class UserCacheHiddenTest(unittest.TestCase):
    def test_put_argument_is_detached(self):
        cache = UserCache()
        payload = {"name": "a", "roles": ["admin"]}
        cache.put("u1", payload)
        payload["name"] = "b"
        payload["roles"].append("root")
        self.assertEqual(
            cache.get("u1"), {"name": "a", "roles": ["admin"]}
        )
'''
        },
    ),
    _task(
        "hard/debug/transaction_atomicity",
        DEBUG,
        "`Service.import_items` 必须原子：任一 key 为空或 value 为 None 时，"
        "整批都不写入，并抛 ValueError。",
        {
            "repo.py": (
                "class Repo:\n"
                "    def __init__(self):\n"
                "        self.items = {}\n\n"
                "    def save(self, key, value):\n"
                "        self.items[key] = value\n"
            ),
            "service.py": (
                "from repo import Repo\n\n\n"
                "class Service:\n"
                "    def __init__(self, repo):\n"
                "        self.repo = repo\n\n"
                "    def import_items(self, pairs):\n"
                "        for key, value in pairs:\n"
                "            self.repo.save(key, value)\n"
            ),
        },
        {
            "test_service.py": '''\
import unittest

from repo import Repo
from service import Service


class ServiceTest(unittest.TestCase):
    def test_imports_valid_pairs(self):
        repo = Repo()
        Service(repo).import_items([("a", 1), ("b", 2)])
        self.assertEqual(repo.items, {"a": 1, "b": 2})

    def test_invalid_pair_leaves_repo_untouched(self):
        repo = Repo()
        with self.assertRaises(ValueError):
            Service(repo).import_items([("a", 1), ("b", None)])
        self.assertEqual(repo.items, {})
'''
        },
        {
            "repo.py": (
                "class Repo:\n"
                "    def __init__(self):\n"
                "        self.items = {}\n\n"
                "    def save(self, key, value):\n"
                "        self.items[key] = value\n"
            ),
            "service.py": (
                "from repo import Repo\n\n\n"
                "class Service:\n"
                "    def __init__(self, repo):\n"
                "        self.repo = repo\n\n"
                "    def import_items(self, pairs):\n"
                "        prepared = []\n"
                "        for key, value in pairs:\n"
                "            if not str(key or \"\").strip():\n"
                "                raise ValueError(\"key is required\")\n"
                "            if value is None:\n"
                "                raise ValueError(\"value is required\")\n"
                "            prepared.append((key, value))\n"
                "        for key, value in prepared:\n"
                "            self.repo.save(key, value)\n"
            ),
        },
        {
            "test_service_hidden.py": '''\
import unittest

from repo import Repo
from service import Service


class ServiceHiddenTest(unittest.TestCase):
    def test_empty_key_is_atomic(self):
        repo = Repo()
        with self.assertRaises(ValueError):
            Service(repo).import_items([("a", 1), ("", 2)])
        self.assertEqual(repo.items, {})
'''
        },
    ),
    # ------------------------------------------------------------------
    # 性能 / 批量
    # ------------------------------------------------------------------
    _task(
        "hard/performance/batch_fetch",
        PERFORMANCE,
        "`names(repo, keys)` 必须批量读取：一次 repo 调用返回所有值，"
        "不能对每个 key 单独 get（N+1）。",
        {
            "repo.py": (
                "class Repo:\n"
                "    def __init__(self, rows):\n"
                "        self.rows = dict(rows)\n"
                "        self.calls = 0\n\n"
                "    def get(self, key):\n"
                "        self.calls += 1\n"
                "        return self.rows.get(key)\n"
            ),
            "service.py": (
                "def names(repo, keys):\n"
                "    return [repo.get(key) for key in keys]\n"
            ),
        },
        {
            "test_service.py": '''\
import unittest

from repo import Repo
from service import names


class ServiceTest(unittest.TestCase):
    def test_reads_values(self):
        repo = Repo({"a": "A", "b": "B"})
        self.assertEqual(names(repo, ["a", "b"]), ["A", "B"])

    def test_uses_a_single_batch_call(self):
        repo = Repo({"a": "A", "b": "B", "c": "C"})
        self.assertEqual(names(repo, ["a", "b", "c"]), ["A", "B", "C"])
        self.assertLessEqual(repo.calls, 1)
'''
        },
        {
            "repo.py": (
                "class Repo:\n"
                "    def __init__(self, rows):\n"
                "        self.rows = dict(rows)\n"
                "        self.calls = 0\n\n"
                "    def get(self, key):\n"
                "        self.calls += 1\n"
                "        return self.rows.get(key)\n\n"
                "    def get_many(self, keys):\n"
                "        self.calls += 1\n"
                "        return [self.rows.get(key) for key in keys]\n"
            ),
            "service.py": (
                "def names(repo, keys):\n"
                "    return repo.get_many(keys)\n"
            ),
        },
        {
            "test_service_hidden.py": '''\
import unittest

from repo import Repo
from service import names


class ServiceHiddenTest(unittest.TestCase):
    def test_large_batch(self):
        keys = [f"k{index}" for index in range(200)]
        repo = Repo({key: index for index, key in enumerate(keys)})
        self.assertEqual(names(repo, keys), list(range(200)))
        self.assertLessEqual(repo.calls, 1)
'''
        },
    ),
    _task(
        "hard/performance/lru_cache",
        PERFORMANCE,
        "实现容量受限的 LRU：get 命中后刷新最近使用顺序；put 超容量时淘汰最久未"
        "使用的键；capacity 为 0 时什么都不存；capacity 为负数抛 ValueError。",
        {
            "lru.py": (
                "class LRUCache:\n"
                "    def __init__(self, capacity):\n"
                "        self.capacity = capacity\n"
                "        self._data = {}\n\n"
                "    def get(self, key):\n"
                "        return self._data.get(key)\n\n"
                "    def put(self, key, value):\n"
                "        self._data[key] = value\n"
            )
        },
        {
            "test_lru.py": '''\
import unittest

from lru import LRUCache


class LruTest(unittest.TestCase):
    def test_basic_get_put(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("missing"))

    def test_evicts_least_recently_used(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")
        cache.put("c", 3)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.get("c"), 3)
'''
        },
        {
            "lru.py": (
                "from collections import OrderedDict\n\n\n"
                "class LRUCache:\n"
                "    def __init__(self, capacity):\n"
                "        if capacity < 0:\n"
                "            raise ValueError(\"capacity must be non-negative\")\n"
                "        self.capacity = capacity\n"
                "        self._data = OrderedDict()\n\n"
                "    def get(self, key):\n"
                "        if key not in self._data:\n"
                "            return None\n"
                "        self._data.move_to_end(key)\n"
                "        return self._data[key]\n\n"
                "    def put(self, key, value):\n"
                "        if self.capacity == 0:\n"
                "            return\n"
                "        if key in self._data:\n"
                "            self._data.move_to_end(key)\n"
                "        self._data[key] = value\n"
                "        if len(self._data) > self.capacity:\n"
                "            self._data.popitem(last=False)\n"
            )
        },
        {
            "test_lru_hidden.py": '''\
import unittest

from lru import LRUCache


class LruHiddenTest(unittest.TestCase):
    def test_update_refreshes_and_zero_capacity(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("a", 9)
        cache.put("c", 3)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), 9)
        self.assertEqual(cache.get("c"), 3)

        empty = LRUCache(0)
        empty.put("x", 1)
        self.assertIsNone(empty.get("x"))

    def test_negative_capacity_is_rejected(self):
        with self.assertRaises(ValueError):
            LRUCache(-1)
'''
        },
    ),
]
