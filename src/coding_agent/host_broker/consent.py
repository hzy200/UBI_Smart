"""A2L 本地同意：Phase 1 的最小实现。

生产桌面应换成托盘/本地面板；这里先提供 stdin 与可注入回调，保证
grant.request 走本地人工确认，而不是由服务端直接放行。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Protocol

from coding_agent.host_broker.protocol import Frame

_LOG = logging.getLogger(__name__)

# Tk 不是线程安全的：多个授权请求并发时串行弹窗，避免抢同一个解释器。
_TK_LOCK = threading.Lock()

_VERB_LABELS = {
    "read": "读取文件",
    "list": "列出目录",
    "find": "查找文件",
    "write": "写入/修改文件",
    "exec": "执行命令",
}


def _verb_text(verbs: list[str]) -> str:
    labels = [_VERB_LABELS.get(v, str(v)) for v in verbs]
    return "、".join(labels) if labels else "（未说明）"


def _ttl_text(ttl_s: int) -> str:
    if ttl_s <= 0:
        return "仅本次请求"
    if ttl_s % 3600 == 0:
        return f"{ttl_s // 3600} 小时"
    if ttl_s % 60 == 0:
        return f"{ttl_s // 60} 分钟"
    return f"{ttl_s} 秒"

ConsentCallback = Callable[[Frame], bool | Awaitable[bool]]


class ConsentProvider(Protocol):
    async def ask(self, request: Frame) -> bool: ...


class AutoConsent:
    """测试/演示用；生产不要默认使用。"""

    def __init__(self, allow: bool = True) -> None:
        self.allow = bool(allow)

    async def ask(self, request: Frame) -> bool:
        return self.allow


class CallbackConsent:
    def __init__(self, callback: ConsentCallback) -> None:
        self.callback = callback

    async def ask(self, request: Frame) -> bool:
        result = self.callback(request)
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)


class TkConsent:
    """本机授权对话框（tkinter，stdlib）。

    设计原则：默认拒绝、关闭窗口即拒绝、敏感路径需额外确认；
    所有展示内容只来自 grant.request，不访问工作区文件。
    """

    async def ask(self, request: Frame) -> bool:
        return await asyncio.to_thread(self._ask_sync, request)

    def _ask_sync(self, request: Frame) -> bool:
        try:
            import tkinter as tk
            from tkinter import font as tkfont
        except Exception:  # noqa: BLE001 - headless/精简环境
            return False
        payload = dict(request.payload or {})
        scope = [str(item) for item in (payload.get("scope") or [])]
        verbs = [str(item) for item in (payload.get("verbs") or [])]
        ttl_s = int(payload.get("ttl_s") or 0)
        reason = str(payload.get("reason") or "").strip()
        sensitive = bool(payload.get("sensitive"))
        result = {"allow": False}
        with _TK_LOCK:
            try:
                root = tk.Tk()
                self._build(
                    tk, tkfont, root, result,
                    scope=scope,
                    verbs=verbs,
                    ttl_s=ttl_s,
                    reason=reason,
                    sensitive=sensitive,
                )
                root.mainloop()
            except Exception:  # noqa: BLE001 - GUI 不可用时 fail closed
                return False
        return bool(result["allow"])

    def _build(
        self,
        tk,
        tkfont,
        root,
        result: dict[str, bool],
        *,
        scope: list[str],
        verbs: list[str],
        ttl_s: int,
        reason: str,
        sensitive: bool,
    ) -> None:
        bg = "#f4f6fb"
        card = "#ffffff"
        text = "#1d2433"
        muted = "#65708a"
        border = "#d8dfeb"
        accent = "#2563eb"
        bad = "#c0392b"

        root.title("CodingAgent · 本机文件访问请求")
        root.configure(bg=bg)
        root.resizable(False, False)
        root.attributes("-topmost", True)

        title_font = tkfont.Font(family="Microsoft YaHei UI", size=13, weight="bold")
        body_font = tkfont.Font(family="Microsoft YaHei UI", size=10)
        mono_font = tkfont.Font(family="Consolas", size=9)

        outer = tk.Frame(root, bg=bg, padx=18, pady=16)
        outer.pack(fill="both", expand=True)

        head = tk.Frame(outer, bg=bg)
        head.pack(fill="x", anchor="w")
        tk.Label(
            head, text="CodingAgent", bg=bg, fg=accent,
            font=tkfont.Font(family="Microsoft YaHei UI", size=9, weight="bold"),
        ).pack(anchor="w")
        tk.Label(
            head, text="本机文件访问请求", bg=bg, fg=text, font=title_font,
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            head,
            text="以下操作只在你自己的电脑上执行。拒绝不会影响其他功能。",
            bg=bg, fg=muted, font=body_font,
        ).pack(anchor="w", pady=(4, 0))

        box = tk.Frame(
            outer, bg=card, highlightbackground=border,
            highlightthickness=1, padx=14, pady=12,
        )
        box.pack(fill="both", expand=True, pady=(12, 0))

        def row(label: str, value: str, *, mono: bool = False) -> None:
            line = tk.Frame(box, bg=card)
            line.pack(fill="x", pady=3)
            tk.Label(
                line, text=label, width=7, anchor="nw",
                bg=card, fg=muted, font=body_font,
            ).pack(side="left", anchor="n")
            tk.Label(
                line, text=value or "（未提供）", anchor="nw", justify="left",
                wraplength=430, bg=card, fg=text,
                font=mono_font if mono else body_font,
            ).pack(side="left", fill="x", expand=True)

        row("操作", _verb_text(verbs))
        row("有效期", _ttl_text(ttl_s))
        if reason:
            row("原因", reason)

        tk.Label(
            box, text="访问范围", anchor="w",
            bg=card, fg=muted, font=body_font,
        ).pack(fill="x", pady=(8, 2))
        shown = scope[:6]
        for item in shown or ["（未提供路径）"]:
            tk.Label(
                box, text=item, anchor="w", justify="left", wraplength=440,
                bg=card, fg=text, font=mono_font,
            ).pack(fill="x", pady=1)
        if len(scope) > len(shown):
            tk.Label(
                box, text=f"另有 {len(scope) - len(shown)} 个路径",
                anchor="w", bg=card, fg=muted, font=body_font,
            ).pack(fill="x", pady=(2, 0))

        confirmed = tk.BooleanVar(value=not sensitive)
        sensitive_check = None
        if sensitive:
            warn = tk.Frame(box, bg="#fdecea", padx=10, pady=8)
            warn.pack(fill="x", pady=(10, 0))
            tk.Label(
                warn, text="敏感位置：请确认该路径确实属于当前项目。",
                anchor="w", bg="#fdecea", fg=bad, font=body_font,
            ).pack(fill="x")
            sensitive_check = tk.Checkbutton(
                box, text="我确认信任以上范围", variable=confirmed,
                anchor="w", bg=card, fg=text, activebackground=card,
                font=body_font,
            )
            sensitive_check.pack(fill="x", pady=(6, 0))

        footer = tk.Frame(outer, bg=bg)
        footer.pack(fill="x", pady=(12, 0))
        tk.Label(
            footer, text="关闭窗口 = 拒绝", bg=bg, fg=muted, font=body_font,
        ).pack(side="left")

        def _allow() -> None:
            if not confirmed.get():
                return
            result["allow"] = True
            root.destroy()

        def _deny() -> None:
            result["allow"] = False
            root.destroy()

        allow_btn = tk.Button(
            footer, text="允许本次", width=10, command=_allow,
            bg=accent, fg="#ffffff", activebackground="#1d4ed8",
            activeforeground="#ffffff", relief="flat", bd=0,
            font=tkfont.Font(family="Microsoft YaHei UI", size=10, weight="bold"),
            state="normal" if confirmed.get() else "disabled",
            cursor="hand2",
        )
        allow_btn.pack(side="right", padx=(8, 0), ipady=4)
        deny_btn = tk.Button(
            footer, text="拒绝", width=10, command=_deny,
            bg="#e8ecf4", fg=text, activebackground="#d8dfeb",
            relief="flat", bd=0,
            font=tkfont.Font(family="Microsoft YaHei UI", size=10),
            cursor="hand2",
        )
        deny_btn.pack(side="right", ipady=4)

        if sensitive_check is not None:
            sensitive_check.configure(
                command=lambda: allow_btn.configure(
                    state="normal" if confirmed.get() else "disabled"
                )
            )

        root.protocol("WM_DELETE_WINDOW", _deny)
        root.bind("<Escape>", lambda _event: _deny())
        deny_btn.focus_set()

        root.update_idletasks()
        width = 560
        height = root.winfo_reqheight()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.lift()
        try:
            root.focus_force()
        except Exception:  # noqa: BLE001 - 个别窗口管理器拒绝抢焦点
            pass


class RemoteConsent:
    """把授权请求转到桌面 A2，由聊天界面里的按钮决定。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 600.0,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "")
        self.poll_s = max(0.1, float(poll_s))
        self.timeout_s = max(1.0, float(timeout_s))
        # 内部 loopback 调用不能走系统代理。
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    async def ask(self, request: Frame) -> bool:
        return await asyncio.to_thread(self._ask_sync, request)

    def _ask_sync(self, request: Frame) -> bool:
        payload = dict(request.payload or {})
        body = json.dumps(
            {
                "scope": payload.get("scope") or [],
                "verbs": payload.get("verbs") or [],
                "ttl_s": int(payload.get("ttl_s") or 0),
                "reason": str(payload.get("reason") or ""),
                "sensitive": bool(payload.get("sensitive")),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "X-Consent-Token": self.token,
            "Content-Type": "application/json",
        }
        try:
            req = urllib.request.Request(
                f"{self.base_url}/internal/consent",
                data=body,
                method="POST",
                headers=headers,
            )
            with self._opener.open(req, timeout=10) as response:
                consent_id = str(
                    json.loads(response.read()).get("id") or ""
                )
        except Exception as exc:  # noqa: BLE001 - fail closed
            _LOG.warning(
                "A2L 授权桥不可用：%s: %s", type(exc).__name__, exc
            )
            return False
        if not consent_id:
            return False
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/internal/consent/{consent_id}",
                    headers={"X-Consent-Token": self.token},
                )
                with self._opener.open(req, timeout=10) as response:
                    state = str(
                        json.loads(response.read()).get("state") or ""
                    )
            except Exception:  # noqa: BLE001 - 下一轮重试
                time.sleep(self.poll_s)
                continue
            if state == "allowed":
                return True
            if state == "denied":
                return False
            if state == "missing":
                return False
            time.sleep(self.poll_s)
        return False


class StdinConsent:
    """在 A2L 子进程的控制台里询问 y/N；不阻塞事件循环。"""

    async def ask(self, request: Frame) -> bool:
        payload = dict(request.payload or {})
        scope = payload.get("scope") or []
        verbs = payload.get("verbs") or []
        reason = str(payload.get("reason") or "")
        sensitive = bool(payload.get("sensitive"))
        prompt = (
            "\n[A2L] 收到本地授权请求\n"
            f"  范围: {scope}\n"
            f"  动词: {verbs}\n"
            f"  原因: {reason}\n"
            f"  敏感: {'是' if sensitive else '否'}\n"
            "允许? [y/N] "
        )
        answer = await asyncio.to_thread(input, prompt)
        return str(answer).strip().lower() in {"y", "yes"}
