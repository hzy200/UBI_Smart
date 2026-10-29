"""最小 SMTP 投递：只用于自助密码重置。

失败不抛出（调用方总是返回同样的 200，避免账号枚举），但会计数并写日志。
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from coding_agent.platform.observability import note_silent_failure

_LOG = logging.getLogger(__name__)


def send_password_reset(
    settings: object,
    to_address: str,
    token_or_link: str,
) -> bool:
    host = str(getattr(settings, "smtp_host", "") or "").strip()
    target = str(to_address or "").strip()
    if not host or not target:
        return False
    sender = str(getattr(settings, "smtp_from", "") or "").strip() or (
        str(getattr(settings, "smtp_user", "") or "").strip() or host
    )
    message = EmailMessage()
    message["Subject"] = "CodingAgent 密码重置"
    message["From"] = sender
    message["To"] = target
    message.set_content(
        "你请求重置 CodingAgent 密码。\n\n"
        f"{token_or_link}\n\n"
        "如果不是你本人操作，请忽略本邮件。链接/令牌默认 30 分钟内有效。"
    )
    try:
        with smtplib.SMTP(
            host,
            int(getattr(settings, "smtp_port", 587) or 587),
            timeout=10,
        ) as smtp:
            smtp.ehlo()
            if bool(getattr(settings, "smtp_starttls", True)):
                smtp.starttls()
                smtp.ehlo()
            user = str(getattr(settings, "smtp_user", "") or "")
            password = str(getattr(settings, "smtp_password", "") or "")
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 - 不向调用方泄露投递细节
        note_silent_failure("password_reset_mail")
        _LOG.warning("密码重置邮件发送失败：%s", type(exc).__name__)
        return False
    return True
