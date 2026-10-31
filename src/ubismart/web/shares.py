"""分享快照脱敏与只读 HTML 渲染。

分享页是匿名可访问的：只保留 user/assistant 文本与工具摘要，去掉工具原始
输出、system 消息、workspace 路径与常见秘密。渲染只做文本转义，不注入 HTML。
"""

from __future__ import annotations

import html
import re
from typing import Any

from ubismart.security.secrets import redact_secrets

MAX_MESSAGES = 200
MAX_TEXT = 8000
_ABS_PATH = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"'<>]+|"
    r"/(?:home|Users|mnt|tmp|var|opt|root|workspace|projects?)/"
    r"[^\s\"'<>]+)"
)


def redact_text(value: Any, *, workspace: str = "") -> str:
    text = str(value or "")
    if workspace:
        text = text.replace(workspace, "<workspace>")
    text = _ABS_PATH.sub("<path>", text)
    return redact_secrets(text)[:MAX_TEXT]


def build_share_payload(
    messages: list[dict[str, Any]],
    *,
    title: str = "",
    workspace: str = "",
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for raw in messages or []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "")
        if role == "工具" or raw.get("kind") == "tool":
            items.append(
                {
                    "role": "工具",
                    "name": str(raw.get("name") or "")[:64],
                    "summary": redact_text(
                        raw.get("summary"), workspace=workspace
                    )[:1000],
                    "state": str(raw.get("state") or "")[:16],
                    "ok": raw.get("ok"),
                }
            )
        elif role in {"user", "assistant"}:
            text = redact_text(
                raw.get("text"), workspace=workspace
            )
            if not text.strip():
                continue
            items.append(
                {
                    "role": role,
                    "phase": str(raw.get("phase") or "")[:16],
                    "text": text,
                }
            )
        if len(items) >= MAX_MESSAGES:
            break
    return {
        "title": str(title or "")[:160],
        "messages": items,
    }


def _role_label(role: str) -> str:
    if role == "user":
        return "提问"
    if role == "assistant":
        return "回答"
    return "工具"


def render_share_html(data: dict[str, Any]) -> str:
    title = html.escape(str(data.get("title") or "UBISmart 会话"))
    created = html.escape(str(data.get("created_at") or ""))
    expires = html.escape(str(data.get("expires_at") or ""))
    blocks: list[str] = []
    for item in data.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        body = (
            html.escape(str(item.get("summary") or ""))
            if role == "工具"
            else html.escape(str(item.get("text") or ""))
        )
        label = _role_label(role)
        meta = ""
        if role == "工具" and item.get("name"):
            meta = (
                '<span class="share-meta">'
                + html.escape(str(item.get("name")))
                + "</span>"
            )
        css_role = role if role in {"user", "assistant"} else "tool"
        blocks.append(
            f'<article class="share-msg share-{css_role}">'
            f'<header><span class="share-role">{label}</span>{meta}</header>'
            f"<pre>{body}</pre></article>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} · 只读分享</title>
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
<link rel="stylesheet" href="/static/share.css">
</head>
<body>
<main class="share-shell">
<header class="share-head">
<h1>{title}</h1>
<p class="share-note">只读分享 · 创建于 {created} · 过期于 {expires}</p>
</header>
<section class="share-transcript">
{''.join(blocks) or '<p class="share-empty">这份分享没有可展示的消息。</p>'}
</section>
<footer class="share-foot">
由 UBISmart 生成；内容已脱敏，不包含工具原始输出与工作区绝对路径。
</footer>
</main>
</body>
</html>"""
