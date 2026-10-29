"""终端控制序列清洗：只保留 SGR 颜色，删除 OSC/CSI 操作。"""

from __future__ import annotations

from coding_agent.terminal_text import sanitize_terminal_text


def test_plain_text_is_unchanged() -> None:
    assert sanitize_terminal_text("hello\nworld") == "hello\nworld"
    assert sanitize_terminal_text(None) == ""
    assert sanitize_terminal_text(123) == "123"


def test_sgr_color_is_kept() -> None:
    text = "\x1b[31mred\x1b[0m"
    assert sanitize_terminal_text(text) == text
    assert (
        sanitize_terminal_text("\x1b[38;5;196mred\x1b[0m")
        == "\x1b[38;5;196mred\x1b[0m"
    )


def test_cursor_and_screen_control_are_removed() -> None:
    assert sanitize_terminal_text("\x1b[2J\x1b[Hhome") == "home"
    assert sanitize_terminal_text("\x1b[?25lhide\x1b[?25h") == "hide"
    assert sanitize_terminal_text("\x1b[1Aup") == "up"


def test_osc_and_dcs_payloads_are_removed() -> None:
    # OSC 52 剪贴板注入
    assert (
        sanitize_terminal_text("a\x1b]52;c;cm0gLXJm\x07b") == "ab"
    )
    # OSC 标题，以 ST 结束
    assert sanitize_terminal_text("\x1b]0;title\x1b\\ok") == "ok"
    # DCS 查询
    assert sanitize_terminal_text("\x1bP1;2qpayload\x1b\\x") == "x"


def test_c0_controls_are_removed_but_newline_kept() -> None:
    assert sanitize_terminal_text("a\x00b\x08c\td\ne\rf") == "abc\td\nef"
    assert sanitize_terminal_text("tail\x1b") == "tail"
