"""real 套件：真实标准库代码回归。

与 hard 套件不同，这里的源文件不是自造的小仓库，而是**完整标准库模块**
（13–100KB）。任务只在原文里注入一处回归，参考解就是未修改的标准库源码。

设计约束：

- 不把标准库源码 vendor 进仓库：导入时从宿主 Python 读取，按当前环境的
  标准库版本生成任务；锚点必须唯一，版本变化时任务会**显式报错**而不是静默
  生成一个无效任务。
- 判定仍用 stdlib unittest；WSL 侧 Python 3.12 已实测可导入这些 3.11 模块。
- 可见测试覆盖回归本身；隐藏测试补边界情况。
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from harness import (
    DEEP,
    REAL_BLIND,
    REAL_CODE,
    REAL_MULTI,
    REAL_SPEC,
    EvalTask,
)

_SOURCE_CACHE: dict[str, str] = {}


def _source(module: str) -> str:
    if module not in _SOURCE_CACHE:
        imported = importlib.import_module(module)
        path = inspect.getsourcefile(imported)
        if not path:
            raise RuntimeError(f"{module} 没有可读源码")
        text = Path(path).read_text(encoding="utf-8")
        if len(text) < 10_000:
            raise RuntimeError(
                f"{module} 源文件只有 {len(text)} 字符，不像真实代码"
            )
        _SOURCE_CACHE[module] = text
    return _SOURCE_CACHE[module]


def _prompt(text: str) -> str:
    return (
        f"项目就在当前工作目录。{text}\n"
        "请让项目自带的测试全部通过。测试文件不要改"
        "（判定前会被还原），改动请落在源码上。"
    )


def _regression_task(
    *,
    task_id: str,
    module: str,
    prompt: str,
    old: str,
    new: str,
    tests: dict[str, str],
    hidden_tests: dict[str, str],
    category: str = REAL_CODE,
) -> EvalTask:
    original = _source(module)
    count = original.count(old)
    if count != 1:
        raise RuntimeError(
            f"{task_id} 的注入锚点在 {module}.py 出现 {count} 次，"
            "标准库版本可能已变化"
        )
    return EvalTask(
        id=task_id,
        category=category,
        tier=DEEP,
        prompt=_prompt(prompt),
        sources={f"{module}.py": original.replace(old, new, 1)},
        tests=tests,
        hidden_tests=hidden_tests,
        reference={f"{module}.py": original},
        timeout=300,
        agent_timeout=300,
    )


def _blind_prompt(text: str) -> str:
    return (
        f"项目就在当前工作目录。{text}\n"
        "项目里没有现成测试；请根据症状定位并修复，改动落在源码上。"
    )


def _blind_task(
    *,
    task_id: str,
    module: str,
    prompt: str,
    old: str,
    new: str,
    hidden_tests: dict[str, str],
) -> EvalTask:
    original = _source(module)
    count = original.count(old)
    if count != 1:
        raise RuntimeError(
            f"{task_id} 的注入锚点在 {module}.py 出现 {count} 次，"
            "标准库版本可能已变化"
        )
    return EvalTask(
        id=task_id,
        category=REAL_BLIND,
        tier=DEEP,
        prompt=_blind_prompt(prompt),
        sources={f"{module}.py": original.replace(old, new, 1)},
        # 盲测：不给任何可见测试，模型只能从症状出发。
        tests={},
        hidden_tests=hidden_tests,
        reference={f"{module}.py": original},
        timeout=300,
        agent_timeout=300,
    )


def _multi_regression_task(
    *,
    task_id: str,
    module: str,
    prompt: str,
    replacements: tuple[tuple[str, str], ...],
    tests: dict[str, str],
    hidden_tests: dict[str, str],
) -> EvalTask:
    original = _source(module)
    seed = original
    for old, new in replacements:
        count = seed.count(old)
        if count != 1:
            raise RuntimeError(
                f"{task_id} 的注入锚点在 {module}.py 出现 {count} 次，"
                "标准库版本可能已变化"
            )
        seed = seed.replace(old, new, 1)
    return EvalTask(
        id=task_id,
        category=REAL_MULTI,
        tier=DEEP,
        prompt=_prompt(prompt),
        sources={f"{module}.py": seed},
        tests=tests,
        hidden_tests=hidden_tests,
        reference={f"{module}.py": original},
        timeout=300,
        agent_timeout=300,
    )


TASKS_REAL: list[EvalTask] = [
    _regression_task(
        task_id="real/configparser/interpolation",
        module="configparser",
        prompt=(
            "`configparser.py` 是 Python 标准库的完整副本。最近插值处理出现"
            "回归：转义的 `%%` 不再被识别为字面量。请修复源码。"
        ),
        old='            if c == "%":',
        new='            if c == "!":',
        tests={
            "test_configparser.py": '''\
import configparser
import unittest


class ConfigParserTest(unittest.TestCase):
    def test_escaped_percent(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[s]\\nvalue = 100%%\\n")
        self.assertEqual(cfg["s"]["value"], "100%")

    def test_basic_interpolation_still_works(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(
            "[DEFAULT]\\nwho = world\\n[s]\\ngreeting = hello %(who)s\\n"
        )
        self.assertEqual(cfg["s"]["greeting"], "hello world")
'''
        },
        hidden_tests={
            "test_configparser_hidden.py": '''\
import configparser
import unittest


class ConfigParserHiddenTest(unittest.TestCase):
    def test_recursive_interpolation(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(
            "[DEFAULT]\\na = %(b)s\\nb = ok\\n[s]\\nx = %(a)s\\n"
        )
        self.assertEqual(cfg["s"]["x"], "ok")

    def test_missing_option_still_raises(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[s]\\nbad = %(missing)s\\n")
        with self.assertRaises(
            configparser.InterpolationMissingOptionError
        ):
            cfg["s"]["bad"]
'''
        },
    ),
    _regression_task(
        task_id="real/textwrap/long_words",
        module="textwrap",
        prompt=(
            "`textwrap.py` 是 Python 标准库的完整副本。最近 `break_long_words=True` "
            "时长词不再被断开。请修复源码。"
        ),
        old="        if self.break_long_words:",
        new="        if False:",
        tests={
            "test_textwrap.py": '''\
import textwrap
import unittest


class TextwrapTest(unittest.TestCase):
    def test_breaks_long_words(self):
        self.assertEqual(
            textwrap.fill("abcdefghij", width=5, break_long_words=True),
            "abcde\\nfghij",
        )

    def test_keeps_long_words_intact_when_disabled(self):
        self.assertEqual(
            textwrap.fill("abcdefghij", width=5, break_long_words=False),
            "abcdefghij",
        )
'''
        },
        hidden_tests={
            "test_textwrap_hidden.py": '''\
import textwrap
import unittest


class TextwrapHiddenTest(unittest.TestCase):
    def test_breaks_on_hyphen(self):
        self.assertEqual(
            textwrap.fill("goof-ball", width=5, break_long_words=True),
            "goof-\\nball",
        )

    def test_subsequent_indent(self):
        self.assertEqual(
            textwrap.fill(
                "one two three", width=7, subsequent_indent="  "
            ),
            "one two\\n  three",
        )
'''
        },
    ),
    _regression_task(
        task_id="real/csv/restkey",
        module="csv",
        prompt=(
            "`csv.py` 是 Python 标准库的完整副本。最近 `DictReader` 不再把多余的"
            "列放进 `restkey`。请修复源码。"
        ),
        old="        if lf < lr:",
        new="        if False:",
        tests={
            "test_csv.py": '''\
import csv
import io
import unittest


class CsvTest(unittest.TestCase):
    def test_restkey_collects_extra_columns(self):
        reader = csv.DictReader(
            io.StringIO("a,b\\n1,2,3\\n"), restkey="extra"
        )
        self.assertEqual(
            next(reader), {"a": "1", "b": "2", "extra": ["3"]}
        )

    def test_restval_fills_missing_columns(self):
        reader = csv.DictReader(io.StringIO("a,b\\n1\\n"), restval="")
        self.assertEqual(next(reader), {"a": "1", "b": ""})
'''
        },
        hidden_tests={
            "test_csv_hidden.py": '''\
import csv
import io
import unittest


class CsvHiddenTest(unittest.TestCase):
    def test_blank_lines_are_skipped(self):
        reader = csv.DictReader(io.StringIO("a,b\\n\\n1,2\\n"))
        self.assertEqual(list(reader), [{"a": "1", "b": "2"}])

    def test_fieldnames_are_exposed(self):
        reader = csv.DictReader(io.StringIO("a,b\\n1,2\\n"))
        self.assertEqual(reader.fieldnames, ["a", "b"])
'''
        },
    ),
    _regression_task(
        task_id="real/shlex/comments",
        module="shlex",
        prompt=(
            "`shlex.py` 是 Python 标准库的完整副本。最近 `comments=True` 时"
            "`#` 注释没有被跳过。请修复源码。"
        ),
        old="""                elif nextchar in self.commenters:
                    self.instream.readline()
                    self.lineno += 1
                elif self.posix and nextchar in self.escape:""",
        new="""                elif False:
                    self.instream.readline()
                    self.lineno += 1
                elif self.posix and nextchar in self.escape:""",
        tests={
            "test_shlex.py": '''\
import shlex
import unittest


class ShlexTest(unittest.TestCase):
    def test_comments_are_stripped(self):
        self.assertEqual(shlex.split("a # comment", comments=True), ["a"])

    def test_comments_kept_when_disabled(self):
        self.assertEqual(
            shlex.split("a # comment"), ["a", "#", "comment"]
        )
'''
        },
        hidden_tests={
            "test_shlex_hidden.py": '''\
import shlex
import unittest


class ShlexHiddenTest(unittest.TestCase):
    def test_comment_only_line(self):
        self.assertEqual(shlex.split("# note\\na", comments=True), ["a"])

    def test_posix_quoting(self):
        self.assertEqual(shlex.split("a 'b c'"), ["a", "b c"])
'''
        },
    ),
    _regression_task(
        task_id="real/argparse/type_conversion",
        module="argparse",
        prompt=(
            "`argparse.py` 是 Python 标准库的完整副本。最近单参数不再应用 "
            "`type=` 转换。请修复源码。"
        ),
        old="            value = self._get_value(action, arg_string)",
        new="            value = arg_string",
        tests={
            "test_argparse.py": '''\
import argparse
import unittest


class ArgparseTest(unittest.TestCase):
    def test_type_conversion(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--count", type=int)
        self.assertEqual(parser.parse_args(["--count", "3"]).count, 3)

    def test_default_is_converted(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--count", type=int, default="4")
        self.assertEqual(parser.parse_args([]).count, 4)
'''
        },
        hidden_tests={
            "test_argparse_hidden.py": '''\
import argparse
import unittest


class ArgparseHiddenTest(unittest.TestCase):
    def test_nargs_conversion(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("nums", nargs="+", type=int)
        self.assertEqual(parser.parse_args(["1", "2"]).nums, [1, 2])

    def test_choices_are_still_checked(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--mode", choices=["a", "b"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--mode", "c"])
'''
        },
    ),
]


# ---------------------------------------------------------------------------
# real 进阶：隐藏需求（可见测试全绿）与同一模块的多处回归。
# ---------------------------------------------------------------------------
TASKS_REAL_ADVANCED: list[EvalTask] = [
    _regression_task(
        task_id="real/spec/statistics/median_even",
        module="statistics",
        category=REAL_SPEC,
        prompt=(
            "项目里的统计模块最近出现回归：对偶数个样本求中位数时，返回的是"
            "偏大的那个中间值，而不是两个中间值的平均。请定位并修复。"
        ),
        old="        return (data[i - 1] + data[i]) / 2",
        new="        return data[i]",
        tests={
            "test_statistics.py": '''\
import statistics
import unittest


class StatisticsTest(unittest.TestCase):
    def test_odd_length(self):
        self.assertEqual(statistics.median([3, 1, 2]), 2)
        self.assertEqual(statistics.median([10]), 10)
'''
        },
        hidden_tests={
            "test_statistics_hidden.py": '''\
import statistics
import unittest


class StatisticsHiddenTest(unittest.TestCase):
    def test_even_length(self):
        self.assertEqual(statistics.median([1, 2, 3, 4]), 2.5)
        self.assertEqual(statistics.median([1, 2]), 1.5)
'''
        },
    ),
    _regression_task(
        task_id="real/spec/fractions/negative_denominator",
        module="fractions",
        category=REAL_SPEC,
        prompt=(
            "项目里的分数模块最近出现回归：负分数的负号跑到了分母上，"
            "例如 `1/-2` 而不是 `-1/2`。请定位并修复。"
        ),
        old="            if denominator < 0:",
        new="            if False:",
        tests={
            "test_fractions.py": '''\
import unittest

from fractions import Fraction


class FractionsTest(unittest.TestCase):
    def test_positive_fraction(self):
        value = Fraction(2, 4)
        self.assertEqual(str(value), "1/2")
        self.assertEqual((value.numerator, value.denominator), (1, 2))
'''
        },
        hidden_tests={
            "test_fractions_hidden.py": '''\
import unittest

from fractions import Fraction


class FractionsHiddenTest(unittest.TestCase):
    def test_negative_denominator_is_normalized(self):
        value = Fraction(1, -2)
        self.assertEqual(str(value), "-1/2")
        self.assertEqual((value.numerator, value.denominator), (-1, 2))
        self.assertEqual(value, Fraction(-1, 2))

    def test_double_negative(self):
        value = Fraction(-1, -2)
        self.assertEqual(str(value), "1/2")
        self.assertEqual((value.numerator, value.denominator), (1, 2))
'''
        },
    ),
    _regression_task(
        task_id="real/spec/heapq/reverse_merge",
        module="heapq",
        category=REAL_SPEC,
        prompt=(
            "项目里的堆模块最近出现回归：按降序合并两个已经降序的序列时，"
            "结果没有保持降序。请定位并修复。"
        ),
        old="    if reverse:",
        new="    if False:",
        tests={
            "test_heapq.py": '''\
import heapq
import unittest


class HeapqTest(unittest.TestCase):
    def test_forward_merge(self):
        self.assertEqual(
            list(heapq.merge([1, 3], [2, 4])), [1, 2, 3, 4]
        )
'''
        },
        hidden_tests={
            "test_heapq_hidden.py": '''\
import heapq
import unittest


class HeapqHiddenTest(unittest.TestCase):
    def test_reverse_merge(self):
        self.assertEqual(
            list(heapq.merge([3, 1], [4, 2], reverse=True)),
            [4, 3, 2, 1],
        )
'''
        },
    ),
    _multi_regression_task(
        task_id="real/multi/configparser/escaped_and_recursive",
        module="configparser",
        prompt=(
            "`configparser.py` 是 Python 标准库的完整副本。插值处理有两处回归："
            "转义的 `%%` 没有被识别为字面量；嵌套插值里引用的值包含 `%` 时也"
            "没有被继续解析。请修复源码。"
        ),
        replacements=(
            ('            if c == "%":', '            if c == "!":'),
            ('                if "%" in v:', "                if False:"),
        ),
        tests={
            "test_configparser.py": '''\
import configparser
import unittest


class ConfigParserTest(unittest.TestCase):
    def test_escaped_percent(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[s]\\nvalue = 100%%\\n")
        self.assertEqual(cfg["s"]["value"], "100%")

    def test_basic_interpolation(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(
            "[DEFAULT]\\nwho = world\\n[s]\\ngreeting = hello %(who)s\\n"
        )
        self.assertEqual(cfg["s"]["greeting"], "hello world")
'''
        },
        hidden_tests={
            "test_configparser_hidden.py": '''\
import configparser
import unittest


class ConfigParserHiddenTest(unittest.TestCase):
    def test_recursive_interpolation(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(
            "[DEFAULT]\\na = %(b)s\\nb = ok\\n[s]\\nx = %(a)s\\n"
        )
        self.assertEqual(cfg["s"]["x"], "ok")
'''
        },
    ),
    _multi_regression_task(
        task_id="real/multi/argparse/type_and_default",
        module="argparse",
        prompt=(
            "`argparse.py` 是 Python 标准库的完整副本。`type=` 转换有两处回归："
            "单参数没有转换；默认值（字符串）也没有转换。请修复源码。"
        ),
        replacements=(
            (
                "            value = self._get_value(action, arg_string)",
                "            value = arg_string",
            ),
            (
                "                value = self._get_value(action, value)",
                "                value = value",
            ),
        ),
        tests={
            "test_argparse.py": '''\
import argparse
import unittest


class ArgparseTest(unittest.TestCase):
    def test_type_conversion(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--count", type=int)
        self.assertEqual(parser.parse_args(["--count", "3"]).count, 3)
'''
        },
        hidden_tests={
            "test_argparse_hidden.py": '''\
import argparse
import unittest


class ArgparseHiddenTest(unittest.TestCase):
    def test_default_is_converted(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--count", type=int, default="4")
        self.assertEqual(parser.parse_args([]).count, 4)

    def test_nargs_conversion(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("nums", nargs="+", type=int)
        self.assertEqual(parser.parse_args(["1", "2"]).nums, [1, 2])
'''
        },
    ),
    _multi_regression_task(
        task_id="real/multi/csv/restkey_and_restval",
        module="csv",
        prompt=(
            "`csv.py` 是 Python 标准库的完整副本。`DictReader` 有两处回归："
            "多余列应放进 `restkey`；缺失列应填入 `restval`。请修复源码。"
        ),
        replacements=(
            ("        if lf < lr:", "        if False:"),
            ("                d[key] = self.restval", "                d[key] = None"),
        ),
        tests={
            "test_csv.py": '''\
import csv
import io
import unittest


class CsvTest(unittest.TestCase):
    def test_restkey_collects_extra_columns(self):
        reader = csv.DictReader(
            io.StringIO("a,b\\n1,2,3\\n"), restkey="extra"
        )
        self.assertEqual(
            next(reader), {"a": "1", "b": "2", "extra": ["3"]}
        )
'''
        },
        hidden_tests={
            "test_csv_hidden.py": '''\
import csv
import io
import unittest


class CsvHiddenTest(unittest.TestCase):
    def test_restval_fills_missing_columns(self):
        reader = csv.DictReader(io.StringIO("a,b\\n1\\n"), restval="")
        self.assertEqual(next(reader), {"a": "1", "b": ""})
'''
        },
    ),
    _regression_task(
        task_id="real/spec/textwrap/replace_whitespace",
        module="textwrap",
        category=REAL_SPEC,
        prompt=(
            "项目里的文本排版模块最近出现回归：对包含换行的文本排版时，"
            "换行被原样保留，没有被当作空白处理。请定位并修复。"
        ),
        old="        if self.replace_whitespace:",
        new="        if False:",
        tests={
            "test_textwrap.py": '''\
import textwrap
import unittest


class TextwrapTest(unittest.TestCase):
    def test_plain_spaces(self):
        self.assertEqual(textwrap.fill("a  b", width=10), "a  b")
        self.assertEqual(
            textwrap.fill("hello world", width=5), "hello\\nworld"
        )
'''
        },
        hidden_tests={
            "test_textwrap_hidden.py": '''\
import textwrap
import unittest


class TextwrapHiddenTest(unittest.TestCase):
    def test_newline_becomes_space(self):
        self.assertEqual(textwrap.fill("a\\nb", width=10), "a b")
'''
        },
    ),
    _regression_task(
        task_id="real/spec/argparse/allow_abbrev",
        module="argparse",
        category=REAL_SPEC,
        prompt=(
            "项目里的命令行解析模块最近出现回归：用户输入长选项的缩写"
            "（例如 `--cou` 对应 `--count`）时被当成未知选项。请定位并修复。"
        ),
        old="            if self.allow_abbrev:",
        new="            if False:",
        tests={
            "test_argparse.py": '''\
import argparse
import unittest


class ArgparseTest(unittest.TestCase):
    def test_full_option(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--count", type=int)
        self.assertEqual(parser.parse_args(["--count", "3"]).count, 3)
'''
        },
        hidden_tests={
            "test_argparse_hidden.py": '''\
import argparse
import unittest


class ArgparseHiddenTest(unittest.TestCase):
    def test_abbreviated_option(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--count", type=int)
        self.assertEqual(parser.parse_args(["--cou", "3"]).count, 3)
'''
        },
    ),
    _regression_task(
        task_id="real/spec/heapq/nlargest_key",
        module="heapq",
        category=REAL_SPEC,
        prompt=(
            "项目里的堆模块最近出现回归：按对象的某个字段取最大的 N 个时，"
            "结果不是该字段最大的 N 个。请定位并修复。"
        ),
        old="        if top < k:",
        new="        if False:",
        tests={
            "test_heapq.py": '''\
import heapq
import unittest


class HeapqTest(unittest.TestCase):
    def test_nlargest_without_key(self):
        self.assertEqual(heapq.nlargest(2, [3, 1, 2]), [3, 2])
'''
        },
        hidden_tests={
            "test_heapq_hidden.py": '''\
import heapq
import unittest


class HeapqHiddenTest(unittest.TestCase):
    def test_nlargest_with_key(self):
        items = [("a", 1), ("b", 3), ("c", 2)]
        self.assertEqual(
            heapq.nlargest(2, items, key=lambda item: item[1]),
            [("b", 3), ("c", 2)],
        )
'''
        },
    ),
    _regression_task(
        task_id="real/spec/statistics/variance_xbar",
        module="statistics",
        category=REAL_SPEC,
        prompt=(
            "项目里的统计模块最近出现回归：调用方已经算好均值并传入时，"
            "样本方差会明显偏大；不传均值时正常。请定位并修复。"
        ),
        old="        T, ssd, count = _sum((d := x - c) * d for x in data)",
        new="        T, ssd, count = _sum((d := x + c) * d for x in data)",
        tests={
            "test_statistics.py": '''\
import statistics
import unittest


class StatisticsTest(unittest.TestCase):
    def test_variance_without_xbar(self):
        self.assertEqual(statistics.variance([1, 2, 3]), 1)
'''
        },
        hidden_tests={
            "test_statistics_hidden.py": '''\
import statistics
import unittest


class StatisticsHiddenTest(unittest.TestCase):
    def test_variance_with_xbar(self):
        self.assertEqual(statistics.variance([1, 2, 3], xbar=2), 1)
        self.assertEqual(statistics.variance([2, 4, 6], xbar=4), 4)
'''
        },
    ),
    _regression_task(
        task_id="real/spec/fractions/limit_denominator",
        module="fractions",
        category=REAL_SPEC,
        prompt=(
            "项目里的分数模块最近出现回归：把分数近似到最大分母时，结果"
            "不是最接近的那个。例如圆周率近似到最大分母 10 得到 3，"
            "而不是 22/7。请定位并修复。"
        ),
        old="        if abs(bound2 - self) <= abs(bound1-self):",
        new="        if abs(bound2 - self) > abs(bound1-self):",
        tests={
            "test_fractions.py": '''\
import unittest

from fractions import Fraction


class FractionsTest(unittest.TestCase):
    def test_exact_fraction_is_unchanged(self):
        self.assertEqual(Fraction(1, 2).limit_denominator(10), Fraction(1, 2))
'''
        },
        hidden_tests={
            "test_fractions_hidden.py": '''\
import unittest

from fractions import Fraction


class FractionsHiddenTest(unittest.TestCase):
    def test_best_approximation(self):
        self.assertEqual(
            Fraction("3.141592653589793").limit_denominator(10),
            Fraction(22, 7),
        )
        self.assertEqual(
            Fraction(4321, 8765).limit_denominator(10000),
            Fraction(4321, 8765),
        )
'''
        },
    ),
    _regression_task(
        task_id="real/spec/argparse/dest_hyphen",
        module="argparse",
        category=REAL_SPEC,
        prompt=(
            "项目里的命令行解析模块最近出现回归：带连字符的长选项"
            "（如 `--foo-bar`）解析后，调用方取不到下划线形式的参数名。"
            "请定位并修复。"
        ),
        old="            dest = dest.replace('-', '_')",
        new="            dest = dest",
        tests={
            "test_argparse.py": '''\
import argparse
import unittest


class ArgparseTest(unittest.TestCase):
    def test_plain_option(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--count", type=int)
        self.assertEqual(parser.parse_args(["--count", "3"]).count, 3)
'''
        },
        hidden_tests={
            "test_argparse_hidden.py": '''\
import argparse
import unittest


class ArgparseHiddenTest(unittest.TestCase):
    def test_hyphenated_option(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--foo-bar")
        args = parser.parse_args(["--foo-bar", "x"])
        self.assertEqual(getattr(args, "foo_bar"), "x")
'''
        },
    ),
    _regression_task(
        task_id="real/spec/shlex/escaped_space",
        module="shlex",
        category=REAL_SPEC,
        prompt=(
            "项目里的命令行拆分模块最近出现回归：反斜杠转义的空格没有被"
            "当成普通字符，`\\ a` 被拆成了两个词。请定位并修复。"
        ),
        old="""                elif self.posix and nextchar in self.escape:
                    escapedstate = 'a'
                    self.state = nextchar
                elif nextchar in self.wordchars:""",
        new="""                elif False:
                    escapedstate = 'a'
                    self.state = nextchar
                elif nextchar in self.wordchars:""",
        tests={
            "test_shlex.py": '''\
import shlex
import unittest


class ShlexTest(unittest.TestCase):
    def test_plain_words(self):
        self.assertEqual(shlex.split("a b"), ["a", "b"])
'''
        },
        hidden_tests={
            "test_shlex_hidden.py": '''\
import shlex
import unittest


class ShlexHiddenTest(unittest.TestCase):
    def test_escaped_space(self):
        self.assertEqual(shlex.split(r"\\ a"), [" a"])
'''
        },
    ),
    _regression_task(
        task_id="real/spec/csv/extrasaction",
        module="csv",
        category=REAL_SPEC,
        prompt=(
            "项目里的 CSV 写入模块最近出现回归：字典里有列定义之外的字段时，"
            "默认应该报错；现在它静默丢掉了多余字段。请定位并修复。"
        ),
        old='        if self.extrasaction == "raise":',
        new="        if False:",
        tests={
            "test_csv.py": '''\
import csv
import io
import unittest


class CsvTest(unittest.TestCase):
    def test_matching_fields_are_written(self):
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=["a"])
        writer.writerow({"a": 1})
        self.assertEqual(stream.getvalue().strip(), "1")
'''
        },
        hidden_tests={
            "test_csv_hidden.py": '''\
import csv
import io
import unittest


class CsvHiddenTest(unittest.TestCase):
    def test_extra_fields_raise(self):
        writer = csv.DictWriter(io.StringIO(), fieldnames=["a"])
        with self.assertRaises(ValueError):
            writer.writerow({"a": 1, "b": 2})
'''
        },
    ),
]


# ---------------------------------------------------------------------------
# 盲测：没有可见测试，只给症状；隐藏测试判定。
# ---------------------------------------------------------------------------
TASKS_BLIND: list[EvalTask] = [
    _blind_task(
        task_id="blind/configparser/duplicate_section",
        module="configparser",
        prompt=(
            "同一个 section 名在配置文件里出现两次时，后一个会静默覆盖前一个；"
            "严格模式下这应该报错。请定位并修复。"
        ),
        old="                            if self._strict and sectname in elements_added:",
        new="                            if False:",
        hidden_tests={
            "test_configparser_hidden.py": '''\
import configparser
import unittest


class ConfigParserHiddenTest(unittest.TestCase):
    def test_duplicate_section_raises(self):
        cfg = configparser.ConfigParser()
        with self.assertRaises(configparser.DuplicateSectionError):
            cfg.read_string("[s]\\na = 1\\n[s]\\nb = 2\\n")
'''
        },
    ),
    _blind_task(
        task_id="blind/statistics/mode_first",
        module="statistics",
        prompt=(
            "有多个并列众数时，返回了最后一个而不是最先出现的那个。"
            "例如 `['red', 'red', 'green', 'blue', 'blue']` 应返回 `red`。"
        ),
        old="""    pairs = Counter(iter(data)).most_common(1)
    try:
        return pairs[0][0]""",
        new="""    pairs = Counter(iter(data)).most_common()
    try:
        return pairs[-1][0]""",
        hidden_tests={
            "test_statistics_hidden.py": '''\
import statistics
import unittest


class StatisticsHiddenTest(unittest.TestCase):
    def test_first_mode_wins(self):
        self.assertEqual(
            statistics.mode(["red", "red", "green", "blue", "blue"]),
            "red",
        )
        self.assertEqual(statistics.mode([1, 1, 2, 2]), 1)
'''
        },
    ),
    _blind_task(
        task_id="blind/fractions/round_half_even",
        module="fractions",
        prompt=(
            "把分数四舍五入到整数时，恰好一半的值没有按银行家舍入："
            "`5/2` 得到 3 而不是 2。请定位并修复。"
        ),
        old="            elif remainder * 2 > self.denominator:",
        new="            elif remainder * 2 >= self.denominator:",
        hidden_tests={
            "test_fractions_hidden.py": '''\
import unittest

from fractions import Fraction


class FractionsHiddenTest(unittest.TestCase):
    def test_half_to_even(self):
        self.assertEqual(round(Fraction(5, 2)), 2)
        self.assertEqual(round(Fraction(7, 2)), 4)
'''
        },
    ),
    _blind_task(
        task_id="blind/csv/restval_missing",
        module="csv",
        prompt=(
            "把字典写成 CSV 时，缺少的列没有用默认值补全，而是直接报错。"
            "请定位并修复。"
        ),
        old="        return (rowdict.get(key, self.restval) for key in self.fieldnames)",
        new="        return (rowdict[key] for key in self.fieldnames)",
        hidden_tests={
            "test_csv_hidden.py": '''\
import csv
import io
import unittest


class CsvHiddenTest(unittest.TestCase):
    def test_missing_field_uses_restval(self):
        stream = io.StringIO()
        writer = csv.DictWriter(
            stream, fieldnames=["a", "b"], restval="x"
        )
        writer.writerow({"a": 1})
        self.assertEqual(stream.getvalue().strip(), "1,x")
'''
        },
    ),
    _blind_task(
        task_id="blind/heapq/nsmallest_key",
        module="heapq",
        prompt=(
            "按对象的某个字段取最小的 N 个时，结果不是该字段最小的 N 个。"
            "请定位并修复。"
        ),
        old="        if k < top:",
        new="        if False:",
        hidden_tests={
            "test_heapq_hidden.py": '''\
import heapq
import unittest


class HeapqHiddenTest(unittest.TestCase):
    def test_nsmallest_with_key(self):
        items = [("a", 3), ("b", 1), ("c", 2)]
        self.assertEqual(
            heapq.nsmallest(2, items, key=lambda item: item[1]),
            [("b", 1), ("c", 2)],
        )
'''
        },
    ),
]
