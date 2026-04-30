"""Outbound email job: SMTP send with optional STARTTLS + auth.

Runs inside an RQ worker so retries + DLQ are managed by the queue (see
`app/services/queue.py`). Raises `smtplib.SMTPException` on transport
failure so RQ's `Retry` policy kicks in; final exhaustion drops the job
into the failed-job registry.

Same retry envelope as the webhook job: 3 / 9 / 27 minute backoff, then
DLQ.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15


def send_email(
    to: list[str] | str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str | None,
    smtp_password: str | None,
    smtp_use_tls: bool,
    sender: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Send a single multipart email. Returns a small dict on success."""
    if isinstance(to, str):
        to = [to]
    if not to:
        raise ValueError("send_email requires at least one recipient")
    if not sender:
        raise ValueError("send_email requires a sender")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    # Derive Message-ID domain from the sender so it aligns with
    # SPF/DKIM. A mismatch (e.g. sender qms@example.com, Message-ID
    # @qms.local) is a textbook spam-filter trigger.
    sender_domain = sender.split("@", 1)[-1].strip("> ").strip() or "localhost"
    msg["Message-ID"] = make_msgid(domain=sender_domain)
    msg.set_content(body_text or "")
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as server:
        if smtp_use_tls:
            server.starttls()
        if smtp_username and smtp_password:
            server.login(smtp_username, smtp_password)
        server.send_message(msg)

    logger.info(
        "email sent: to=%s subject=%r host=%s:%s", to, subject, smtp_host, smtp_port
    )
    return {"recipients": to, "subject": subject}
