"""Redis Stream buffer between the MQTT bridge and the trigger worker.

The MQTT bridge `XADD`s every parsed reading onto `qms:readings`. A separate
worker process (`flask trigger-worker`) reads via consumer groups (XREADGROUP
+ XACK) so deliveries are at-least-once and survive worker restarts. The
stream is bounded with `MAXLEN ~`, dropping the oldest entries once the cap
is hit — matches the "100k readings / line" buffering rule in
01-architectural-functional-plan.md (§ Risk: loss of MQTT connectivity).

Tests use `fakeredis` so no real Redis is required.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from flask import Flask, current_app

logger = logging.getLogger(__name__)

STREAM_NAME = "qms:readings"
CONSUMER_GROUP = "qms-workers"
MAX_LEN = 100_000
DEFAULT_BLOCK_MS = 5_000
DEFAULT_BATCH = 16


def get_redis(app: Flask | None = None):
    """Return a Redis client.

    If `app.config['REDIS_CLIENT']` is set (tests inject `fakeredis`), reuse it.
    Otherwise build a real client from `REDIS_URL`.
    """
    cfg = (app or current_app).config
    if "REDIS_CLIENT" in cfg and cfg["REDIS_CLIENT"] is not None:
        return cfg["REDIS_CLIENT"]
    import redis  # imported lazily so unit tests can run without it

    return redis.Redis.from_url(cfg["REDIS_URL"], decode_responses=True)


def ensure_consumer_group(
    redis_client, *, stream: str = STREAM_NAME, group: str = CONSUMER_GROUP
) -> None:
    """Idempotently create the consumer group; tolerate BUSYGROUP."""
    try:
        redis_client.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception as exc:  # redis.ResponseError or fakeredis equivalent
        if "BUSYGROUP" not in str(exc):
            raise


def publish_reading(reading: dict[str, Any], app: Flask | None = None) -> str:
    """Publish a parsed MQTT reading onto the stream. Returns the entry ID."""
    redis_client = get_redis(app)
    payload = json.dumps(reading, default=str)
    return redis_client.xadd(
        STREAM_NAME, {"payload": payload}, maxlen=MAX_LEN, approximate=True
    )


def consume(
    consumer: str,
    handler: Callable[[dict[str, Any]], None],
    *,
    app: Flask | None = None,
    block_ms: int = DEFAULT_BLOCK_MS,
    batch: int = DEFAULT_BATCH,
    once: bool = False,
) -> int:
    """Read from the stream and dispatch each entry to `handler(reading)`.

    XACK only on successful processing, so handler exceptions cause the
    entry to remain pending and be re-delivered on the next claim. With
    `once=True` the loop returns after one batch — used by tests.
    """
    redis_client = get_redis(app)
    ensure_consumer_group(redis_client)
    processed = 0
    while True:
        results = redis_client.xreadgroup(
            CONSUMER_GROUP,
            consumer,
            {STREAM_NAME: ">"},
            count=batch,
            block=block_ms,
        ) or []
        empty_batch = True
        for _stream_name, entries in results:
            for entry_id, fields in entries:
                empty_batch = False
                try:
                    payload_field = fields.get("payload") if isinstance(fields, dict) else None
                    if payload_field is None:
                        # Non-text field: extract from raw bytes mapping
                        payload_field = fields[b"payload"].decode("utf-8")  # type: ignore[index]
                    reading = json.loads(payload_field)
                    handler(reading)
                    redis_client.xack(STREAM_NAME, CONSUMER_GROUP, entry_id)
                    processed += 1
                except Exception:
                    logger.exception(
                        "Trigger worker handler failed: id=%s", entry_id
                    )
                    # Don't XACK — entry stays pending and will be reclaimed.
        if once:
            return processed
        if empty_batch:
            # Block timeout elapsed; loop again.
            continue
