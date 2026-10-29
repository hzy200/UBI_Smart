"""出站 Webhook：事件投递、HMAC 签名与 SSRF 校验。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from coding_agent.control.schema import ControlWebhook

_LOG = logging.getLogger(__name__)
EVENTS = ("run_finished", "run_failed", "approval_requested")
_TASKS: set[asyncio.Task] = set()


class WebhookError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_events(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = list(raw or [])
    values = {
        str(item).strip().lower()
        for item in items
        if str(item).strip()
    }
    unknown = values - set(EVENTS)
    if unknown:
        raise WebhookError(
            f"未知事件：{','.join(sorted(unknown))}"
        )
    if not values:
        raise WebhookError("至少选择一个事件")
    return tuple(event for event in EVENTS if event in values)


def validate_webhook_url(url: str) -> str:
    """只允许 https + 公网 IP；阻止回环/私网/链路本地/保留地址。"""
    text = str(url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme != "https":
        raise WebhookError("Webhook 必须是 https URL")
    if parsed.username or parsed.password:
        raise WebhookError("Webhook URL 不能包含凭据")
    host = parsed.hostname or ""
    if not host:
        raise WebhookError("Webhook URL 缺少主机名")
    port = parsed.port or 443
    if port != 443:
        raise WebhookError("Webhook 只允许 443 端口")
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise WebhookError(f"无法解析 Webhook 主机：{exc}") from exc
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise WebhookError("Webhook 主机地址非法") from exc
        if not ip.is_global:
            raise WebhookError("Webhook 不能指向内网/回环地址")
    return text


def _public(row: ControlWebhook) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "events": [
            item for item in str(row.events or "").split(",") if item
        ],
        "enabled": bool(row.enabled),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_sent_at": row.last_sent_at,
        "last_status": int(row.last_status or 0),
        "last_error": row.last_error,
    }


def sign_payload(secret: str, body: bytes) -> str:
    digest = hmac.new(
        str(secret or "").encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


class WebhookStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create(
        self,
        tenant: str,
        *,
        url: str,
        events: Any,
        name: str = "",
    ) -> tuple[dict[str, Any], str]:
        target = validate_webhook_url(url)
        selected = normalize_events(events)
        secret = "whsec_" + secrets.token_urlsafe(24)
        now = _now()
        row = ControlWebhook(
            id=secrets.token_hex(16),
            tenant=str(tenant or ""),
            name=str(name or "").strip()[:64] or "Webhook",
            url=target,
            secret=secret,
            events=",".join(selected),
            enabled=True,
            created_at=now,
            updated_at=now,
            last_sent_at="",
            last_status=0,
            last_error="",
        )
        with Session(self.engine) as session, session.begin():
            session.add(row)
            session.flush()
            return _public(row), secret

    def list(self, tenant: str) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlWebhook)
                .where(ControlWebhook.tenant == str(tenant or ""))
                .order_by(ControlWebhook.created_at.desc())
            ).all()
            return [_public(row) for row in rows]

    def list_for_delivery(
        self, tenant: str, event: str
    ) -> list[dict[str, Any]]:
        """投递专用：带上签名 secret；HTTP API 永不返回 secret。"""
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ControlWebhook).where(
                    ControlWebhook.tenant == str(tenant or ""),
                    ControlWebhook.enabled.is_(True),
                )
            ).all()
        return [
            {**_public(row), "secret": row.secret}
            for row in rows
            if str(event) in str(row.events or "").split(",")
        ]

    def delete(self, tenant: str, webhook_id: str) -> bool:
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlWebhook, str(webhook_id))
            if row is None or row.tenant != str(tenant or ""):
                return False
            session.delete(row)
            return True

    def record_delivery(
        self,
        tenant: str,
        webhook_id: str,
        *,
        status: int,
        error: str = "",
    ) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlWebhook, str(webhook_id))
            if row is None or row.tenant != str(tenant or ""):
                return
            row.last_status = int(status)
            row.last_error = str(error or "")[:255]
            row.last_sent_at = _now()
            row.updated_at = row.last_sent_at


class WebhookService:
    def __init__(self, store: WebhookStore | None) -> None:
        self.store = store

    @property
    def enabled(self) -> bool:
        return self.store is not None

    def send(
        self, tenant: str, event: str, payload: dict[str, Any]
    ) -> int:
        if self.store is None:
            return 0
        body = json.dumps(
            {"event": event, "tenant": tenant, **payload},
            ensure_ascii=False,
        ).encode("utf-8")
        sent = 0
        for hook in self.store.list_for_delivery(tenant, event):
            secret = hook.get("secret", "")
            if not secret:
                continue
            request = Request(
                hook["url"],
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "CodingAgent-Webhook/1",
                    "X-CodingAgent-Event": event,
                    "X-CodingAgent-Signature": sign_payload(
                        secret, body
                    ),
                },
            )
            status = 0
            error = ""
            try:
                with urlopen(request, timeout=5) as response:  # noqa: S310
                    status = int(getattr(response, "status", 0) or 0)
            except HTTPError as exc:
                status = int(exc.code or 0)
                error = f"HTTP {exc.code}"
            except Exception as exc:  # noqa: BLE001 - 投递失败只记录
                error = str(exc)[:255]
            self.store.record_delivery(
                tenant, hook["id"], status=status, error=error
            )
            if 200 <= status < 300:
                sent += 1
            else:
                _LOG.warning(
                    "Webhook 投递失败（%s）：%s", status, hook["url"]
                )
        return sent


def schedule_webhooks(
    service: WebhookService | None,
    tenant: str,
    event: str,
    payload: dict[str, Any],
) -> None:
    if service is None or not service.enabled:
        return
    try:
        task = asyncio.create_task(
            asyncio.to_thread(service.send, tenant, event, payload)
        )
    except RuntimeError:
        service.send(tenant, event, payload)
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
