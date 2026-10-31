"""浏览器 Web Push（VAPID + payload 加密）。

`pywebpush` 是可选依赖：未安装或未配置密钥时 `enabled=False`，前端退回
站内通知与页面内 Notification。私钥只在服务端，配置端点只返回公钥。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

_LOG = logging.getLogger(__name__)

# 保持推送任务的强引用，避免 fire-and-forget 任务被 GC。
_TASKS: set[asyncio.Task] = set()


class PushService:
    def __init__(self, settings: Any, store: Any | None) -> None:
        self.settings = settings
        self.store = store
        self._module: Any = None
        self._checked = False

    @property
    def public_key(self) -> str:
        return str(
            getattr(self.settings, "webpush_public_key", "") or ""
        ).strip()

    @property
    def private_key(self) -> str:
        return str(
            getattr(self.settings, "webpush_private_key", "") or ""
        ).strip()

    @property
    def subject(self) -> str:
        return str(
            getattr(self.settings, "webpush_subject", "")
            or "mailto:admin@localhost"
        ).strip()

    def _load_module(self) -> Any:
        if not self._checked:
            self._checked = True
            try:
                from pywebpush import WebPushException, webpush

                self._module = (webpush, WebPushException)
            except ImportError:
                self._module = None
        return self._module

    @property
    def enabled(self) -> bool:
        return bool(
            self.store is not None
            and self.public_key
            and self.private_key
            and self._load_module() is not None
        )

    def config(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "public_key": self.public_key if self.enabled else "",
        }

    def send(self, tenant: str, payload: dict[str, Any]) -> int:
        """同步发送到该用户的全部订阅；由调用方放进线程执行。"""
        module = self._load_module()
        if module is None or not self.enabled:
            return 0
        webpush, web_push_exception = module
        data = json.dumps(payload, ensure_ascii=False)
        ttl = max(60, int(getattr(self.settings, "webpush_ttl", 3600) or 3600))
        sent = 0
        for subscription in self.store.list_push_subscriptions(tenant):
            endpoint = str(subscription.get("endpoint") or "")
            if not endpoint:
                continue
            info = {
                "endpoint": endpoint,
                "keys": {
                    "p256dh": subscription.get("p256dh") or "",
                    "auth": subscription.get("auth") or "",
                },
            }
            try:
                webpush(
                    subscription_info=info,
                    data=data,
                    vapid_private_key=self.private_key,
                    vapid_claims={"sub": self.subject},
                    ttl=ttl,
                )
                sent += 1
            except web_push_exception as exc:  # type: ignore[misc]
                response = getattr(exc, "response", None)
                status = int(getattr(response, "status_code", 0) or 0)
                if status in {404, 410}:
                    # 订阅已失效：清掉，避免持续失败。
                    self.store.remove_push_subscription(
                        tenant, endpoint=endpoint
                    )
                else:
                    _LOG.warning("Web Push 发送失败（%s）：%s", status, exc)
            except Exception as exc:  # noqa: BLE001 - 推送失败不影响主流程
                _LOG.warning("Web Push 发送异常：%s", exc)
        return sent


def schedule_push(
    push: Any | None, tenant: str, notification: dict[str, Any]
) -> None:
    """在事件循环中发起后台推送；失败只记日志。"""
    if push is None or not getattr(push, "enabled", False):
        return
    payload = {
        "title": notification.get("title", ""),
        "body": notification.get("body", ""),
        "kind": notification.get("kind", "info"),
        "thread_id": notification.get("thread_id", ""),
        "path": notification.get("path", ""),
        "tag": notification.get("id") or notification.get("dedupe_key") or "",
    }
    try:
        task = asyncio.create_task(
            asyncio.to_thread(push.send, tenant, payload)
        )
    except RuntimeError:
        # 没有运行中的事件循环：直接同步发送（测试/脚本路径）。
        push.send(tenant, payload)
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
