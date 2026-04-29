"""RQ queue service for asynchronous responders.

Two queues:
  * `qms:webhooks` — outbound HTTP POSTs (WEBHOOK responder)
  * `qms:emails`   — outbound SMTP sends (EMAIL responder)

Retry schedule mirrors `01-architectural-functional-plan.md` risk
register: 3 / 9 / 27 minute backoff, then DLQ via RQ's failed-job
registry. Both queues share the same retry envelope and the same binary
Redis connection.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, current_app
from rq import Queue, Retry

QUEUE_NAME = "qms:webhooks"
EMAIL_QUEUE_NAME = "qms:emails"
RETRY_INTERVALS_SECONDS = [180, 540, 1620]  # 3, 9, 27 minutes


def _get_binary_redis(app: Flask | None = None):
    """Binary-mode Redis client for RQ. RQ serialises job state as raw
    bytes, which is incompatible with the text-mode (`decode_responses=
    True`) client used by the stream service. Tests inject a binary
    fakeredis at `REDIS_BINARY_CLIENT`; production constructs a fresh
    binary connection from `REDIS_URL`."""
    cfg = (app or current_app).config
    if cfg.get("REDIS_BINARY_CLIENT") is not None:
        return cfg["REDIS_BINARY_CLIENT"]
    import redis

    return redis.Redis.from_url(cfg["REDIS_URL"])


def get_queue(app: Flask | None = None, *, is_async: bool = True) -> Queue:
    """Build (or reuse) the webhook queue. Pass `is_async=False` in tests
    that want synchronous job execution."""
    return Queue(QUEUE_NAME, connection=_get_binary_redis(app), is_async=is_async)


def enqueue_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    app: Flask | None = None,
    queue: Queue | None = None,
):
    """Enqueue a webhook POST with the standard retry policy. Returns the
    RQ Job. The caller normally only cares about `job.id`."""
    q = queue if queue is not None else get_queue(app)
    return q.enqueue(
        "app.jobs.webhook.post_webhook",
        kwargs={"url": url, "payload": payload, "secret": secret},
        retry=Retry(
            max=len(RETRY_INTERVALS_SECONDS), interval=RETRY_INTERVALS_SECONDS
        ),
        result_ttl=86400,
        failure_ttl=86400 * 7,
    )


def get_email_queue(app: Flask | None = None, *, is_async: bool = True) -> Queue:
    """Build (or reuse) the email queue. Same Redis connection as the
    webhook queue; separated so worker concurrency can be tuned per
    transport without one slow recipient blocking the other."""
    return Queue(
        EMAIL_QUEUE_NAME, connection=_get_binary_redis(app), is_async=is_async
    )


def enqueue_email(
    to: list[str] | str,
    subject: str,
    body_text: str,
    *,
    body_html: str | None = None,
    app: Flask | None = None,
    queue: Queue | None = None,
):
    """Enqueue an SMTP send with the standard retry policy. SMTP
    connection details are read from app config (`SMTP_HOST`,
    `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`,
    `SMTP_FROM`) and frozen into the job kwargs at enqueue time, so a
    later config change won't silently retarget already-queued jobs."""
    cfg = (app or current_app).config
    q = queue if queue is not None else get_email_queue(app)
    return q.enqueue(
        "app.jobs.email.send_email",
        kwargs={
            "to": to,
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "smtp_host": cfg.get("SMTP_HOST", "localhost"),
            "smtp_port": int(cfg.get("SMTP_PORT", 25)),
            "smtp_username": cfg.get("SMTP_USERNAME"),
            "smtp_password": cfg.get("SMTP_PASSWORD"),
            "smtp_use_tls": bool(cfg.get("SMTP_USE_TLS", False)),
            "sender": cfg.get("SMTP_FROM", "qms@local"),
        },
        retry=Retry(
            max=len(RETRY_INTERVALS_SECONDS), interval=RETRY_INTERVALS_SECONDS
        ),
        result_ttl=86400,
        failure_ttl=86400 * 7,
    )
