"""RQ queue service for asynchronous responders.

Single queue (`qms:webhooks`) for outbound HTTP POSTs from the WEBHOOK
responder. Retry schedule mirrors `01-architectural-functional-plan.md`
risk register: 3 / 9 / 27 minute backoff, then DLQ via RQ's failed-job
registry.

Reuses `app.services.stream.get_redis()` so tests injecting a fakeredis
client at `app.config['REDIS_CLIENT']` get queue support for free.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, current_app
from rq import Queue, Retry

QUEUE_NAME = "qms:webhooks"
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
