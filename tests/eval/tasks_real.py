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

from harness import DEEP, REAL_CODE, EvalTask

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
        category=REAL_CODE,
        tier=DEEP,
        prompt=_prompt(prompt),
        sources={f"{module}.py": original.replace(old, new, 1)},
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
