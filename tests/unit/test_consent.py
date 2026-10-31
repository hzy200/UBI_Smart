"""A2L 本地授权：回调、远程桥、stdin 与 Tk 面板的失败/成功路径。"""

from __future__ import annotations

import asyncio
import builtins
import json
import sys
import types
from typing import Any

from ubismart.host_broker.consent import (
    AutoConsent,
    CallbackConsent,
    RemoteConsent,
    StdinConsent,
    TkConsent,
    _ttl_text,
    _verb_text,
)
from ubismart.host_broker.protocol import Frame


def _frame(**payload: Any) -> Frame:
    return Frame(type="grant.request", payload=dict(payload))


def test_auto_and_callback_consent() -> None:
    assert asyncio.run(AutoConsent(True).ask(_frame())) is True
    assert asyncio.run(AutoConsent(False).ask(_frame())) is False

    seen: list[Frame] = []

    def sync(request: Frame) -> bool:
        seen.append(request)
        return True

    assert asyncio.run(CallbackConsent(sync).ask(_frame())) is True
    assert seen

    async def async_cb(_request: Frame) -> bool:
        return False

    assert asyncio.run(CallbackConsent(async_cb).ask(_frame())) is False


def test_consent_text_helpers() -> None:
    assert _ttl_text(0) == "仅本次请求"
    assert _ttl_text(7200) == "2 小时"
    assert _ttl_text(300) == "5 分钟"
    assert _ttl_text(17) == "17 秒"
    assert _verb_text(["read", "exec"]) == "读取文件、执行命令"
    assert _verb_text(["custom"]) == "custom"
    assert _verb_text([]) == "（未说明）"


class _Widget:
    created: list[_Widget] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = dict(kwargs)
        self.destroyed = False
        _Widget.created.append(self)

    def pack(self, *args, **kwargs) -> None:
        pass

    def configure(self, *args, **kwargs) -> None:
        self.kwargs.update(kwargs)

    def title(self, *args) -> None:
        pass

    def resizable(self, *args) -> None:
        pass

    def attributes(self, *args) -> None:
        pass

    def protocol(self, *args) -> None:
        pass

    def bind(self, *args) -> None:
        pass

    def update_idletasks(self) -> None:
        pass

    def winfo_reqheight(self) -> int:
        return 300

    def winfo_screenwidth(self) -> int:
        return 1920

    def winfo_screenheight(self) -> int:
        return 1080

    def geometry(self, *args) -> None:
        pass

    def lift(self) -> None:
        pass

    def focus_force(self) -> None:
        pass

    def focus_set(self) -> None:
        pass

    def mainloop(self) -> None:
        pass

    def destroy(self) -> None:
        self.destroyed = True


class _Root(_Widget):
    pass


class _Var:
    def __init__(self, value: bool = False) -> None:
        self.value = bool(value)

    def get(self) -> bool:
        return self.value

    def set(self, value: bool) -> None:
        self.value = bool(value)


def _fake_tk(monkeypatch) -> None:
    _Widget.created = []
    tk = types.ModuleType("tkinter")
    tk.Frame = _Widget
    tk.Label = _Widget
    tk.Button = _Widget
    tk.Checkbutton = _Widget
    tk.BooleanVar = _Var
    tk.Tk = _Root
    font = types.ModuleType("tkinter.font")
    font.Font = lambda *args, **kwargs: object()
    tk.font = font
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.font", font)


def _buttons() -> list[_Widget]:
    return [w for w in _Widget.created if "text" in w.kwargs]


def test_tk_build_allow_and_deny(monkeypatch) -> None:
    _fake_tk(monkeypatch)
    result = {"allow": False}
    consent = TkConsent()
    consent._build(
        sys.modules["tkinter"],
        sys.modules["tkinter.font"],
        _Root(),
        result,
        scope=["/tmp/a"],
        verbs=["read"],
        ttl_s=60,
        reason="",
        sensitive=False,
    )
    allow = next(w for w in _buttons() if w.kwargs["text"] == "允许本次")
    deny = next(w for w in _buttons() if w.kwargs["text"] == "拒绝")
    allow.kwargs["command"]()
    assert result["allow"] is True

    result["allow"] = False
    deny.kwargs["command"]()
    assert result["allow"] is False


def test_tk_build_sensitive_requires_checkbox(monkeypatch) -> None:
    _fake_tk(monkeypatch)
    result = {"allow": False}
    TkConsent()._build(
        sys.modules["tkinter"],
        sys.modules["tkinter.font"],
        _Root(),
        result,
        scope=[f"/p/{i}" for i in range(8)],
        verbs=["write", "exec"],
        ttl_s=7200,
        reason="同步项目",
        sensitive=True,
    )
    check = next(
        w for w in _Widget.created if "variable" in w.kwargs
    )
    allow = next(w for w in _buttons() if w.kwargs["text"] == "允许本次")
    allow.kwargs["command"]()
    assert result["allow"] is False

    check.kwargs["variable"].set(True)
    check.kwargs["command"]()
    allow.kwargs["command"]()
    assert result["allow"] is True


def test_tk_ask_sync_paths(monkeypatch) -> None:
    _fake_tk(monkeypatch)
    assert (
        TkConsent()._ask_sync(
            _frame(scope=["/tmp"], verbs=["read"], ttl_s=0)
        )
        is False
    )

    monkeypatch.setitem(sys.modules, "tkinter", None)
    assert TkConsent()._ask_sync(_frame()) is False


class _Response:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


class _Opener:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def open(self, request, timeout=10):
        self.calls += 1
        item = self.responses.pop(0) if self.responses else {"state": "pending"}
        if isinstance(item, Exception):
            raise item
        return _Response(item)


def _remote(responses: list[Any], **kwargs) -> RemoteConsent:
    consent = RemoteConsent("http://127.0.0.1:9", "tok", **kwargs)
    consent._opener = _Opener(responses)
    return consent


def test_remote_consent_states() -> None:
    request = _frame(scope=["/tmp"], verbs=["read"], ttl_s=60)
    assert _remote([{"id": "c1"}, {"state": "allowed"}])._ask_sync(
        request
    ) is True
    assert _remote([{"id": "c1"}, {"state": "denied"}])._ask_sync(
        request
    ) is False
    assert _remote([{"id": "c1"}, {"state": "missing"}])._ask_sync(
        request
    ) is False
    assert _remote([{"id": ""}])._ask_sync(request) is False
    assert _remote([RuntimeError("bridge down")])._ask_sync(request) is False
    # 轮询偶发失败后恢复。
    assert _remote(
        [{"id": "c1"}, RuntimeError("poll"), {"state": "allowed"}]
    )._ask_sync(request) is True
    # 超时。
    assert _remote(
        [{"id": "c1"}], poll_s=0.1, timeout_s=0.01
    )._ask_sync(request) is False


def test_stdin_consent(monkeypatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda _prompt: "y")
    assert asyncio.run(
        StdinConsent().ask(_frame(scope=["/tmp"], verbs=["read"]))
    ) is True
    monkeypatch.setattr(builtins, "input", lambda _prompt: "n")
    assert asyncio.run(StdinConsent().ask(_frame(sensitive=True))) is False


def test_tk_import_failure(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "tkinter", None)
    assert TkConsent()._ask_sync(_frame()) is False
