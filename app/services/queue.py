"""RQ queue service for asynchronous responders.

Three queues:
  * `qms:webhooks` — outbound HTTP POSTs (WEBHOOK responder)
  * `qms:emails`   — outbound SMTP sends (EMAIL responder)
  * `qms:sms`      — outbound ClickSend SMS sends (SMS responder)

Retry schedule mirrors `01-architectural-functional-plan.md` risk
register: 3 / 9 / 27 minute backoff, then DLQ via RQ's failed-job
registry. All queues share the same retry envelope and the same binary
Redis connection.
"""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, current_app
from rq import Queue, Retry

# See app/services/stream.py for the rationale behind reading the prefix
# at import time. Same env var, same constraints.
_REDIS_KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "qms")

QUEUE_NAME = f"{_REDIS_KEY_PREFIX}:webhooks"
EMAIL_QUEUE_NAME = f"{_REDIS_KEY_PREFIX}:emails"
SMS_QUEUE_NAME = f"{_REDIS_KEY_PREFIX}:sms"

# Retry envelope and TTLs are operational knobs. Tuning down the retry
# tail (e.g. for transient SaaS endpoints) shouldn't require a deploy.
# Comma-separated seconds list, e.g. "180,540,1620" for 3/9/27 min.
RETRY_INTERVALS_SECONDS: list[int] = [
    int(x) for x in os.environ.get("WEBHOOK_RETRY_INTERVALS", "180,540,1620").split(",")
]
RESULT_TTL_SECONDS = int(os.environ.get("JOB_RESULT_TTL_SECONDS", str(86400)))
FAILURE_TTL_SECONDS = int(
    os.environ.get("JOB_FAILURE_TTL_SECONDS", str(86400 * 7))
)


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
        result_ttl=RESULT_TTL_SECONDS,
        failure_ttl=FAILURE_TTL_SECONDS,
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
        result_ttl=RESULT_TTL_SECONDS,
        failure_ttl=FAILURE_TTL_SECONDS,
    )


def get_sms_queue(app: Flask | None = None, *, is_async: bool = True) -> Queue:
    """Build (or reuse) the SMS queue. Same Redis connection as the
    other responder queues."""
    return Queue(
        SMS_QUEUE_NAME, connection=_get_binary_redis(app), is_async=is_async
    )


def enqueue_sms(
    to: list[str] | str,
    body: str,
    *,
    app: Flask | None = None,
    queue: Queue | None = None,
):
    """Enqueue a ClickSend SMS send with the standard retry policy.

    ClickSend credentials (`CLICKSEND_USERNAME`, `CLICKSEND_API_KEY`,
    `CLICKSEND_SOURCE`, `CLICKSEND_BASE_URL`) are read from app config
    and frozen into the job kwargs at enqueue time — same reasoning as
    the email queue: a later config rotation must not silently retarget
    queued jobs."""
    cfg = (app or current_app).config
    q = queue if queue is not None else get_sms_queue(app)
    return q.enqueue(
        "app.jobs.sms.send_sms",
        kwargs={
            "to": to,
            "body": body,
            "username": cfg.get("CLICKSEND_USERNAME", ""),
            "api_key": cfg.get("CLICKSEND_API_KEY", ""),
            "source": cfg.get("CLICKSEND_SOURCE", "QMS"),
            "base_url": cfg.get(
                "CLICKSEND_BASE_URL", "https://rest.clicksend.com/v3"
            ),
        },
        retry=Retry(
            max=len(RETRY_INTERVALS_SECONDS), interval=RETRY_INTERVALS_SECONDS
        ),
        result_ttl=RESULT_TTL_SECONDS,
        failure_ttl=FAILURE_TTL_SECONDS,
    )
