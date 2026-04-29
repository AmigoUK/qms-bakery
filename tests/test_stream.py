"""Redis Stream service: publish + consumer-group plumbing.

Backed by `fakeredis` (configured in conftest as `REDIS_CLIENT`). No real
broker is required.
"""

from __future__ import annotations

import pytest

from app.services import stream as stream_service


def test_publish_reading_appends_entry(app, redis_client):
    with app.app_context():
        entry_id = stream_service.publish_reading(
            {"metric": "temperature", "temperature": 232.5, "scope": "line:LINE_A"}
        )
    assert entry_id  # XADD returns a non-empty id like '171...0'
    assert redis_client.xlen(stream_service.STREAM_NAME) == 1


def test_publish_reading_serializes_payload_as_json(app, redis_client):
    with app.app_context():
        stream_service.publish_reading({"metric": "weight", "weight": 482.1})
    entries = redis_client.xrange(stream_service.STREAM_NAME)
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    payload = fields["payload"]
    import json

    assert json.loads(payload) == {"metric": "weight", "weight": 482.1}


def test_ensure_consumer_group_is_idempotent(redis_client):
    redis_client.xadd(stream_service.STREAM_NAME, {"payload": "{}"})
    stream_service.ensure_consumer_group(redis_client)
    # Calling again must not raise (BUSYGROUP is swallowed).
    stream_service.ensure_consumer_group(redis_client)
    groups = redis_client.xinfo_groups(stream_service.STREAM_NAME)
    assert any(g["name"] == stream_service.CONSUMER_GROUP for g in groups)


def test_consume_dispatches_then_acks(app, redis_client):
    with app.app_context():
        stream_service.publish_reading({"metric": "x", "x": 1.0})
        stream_service.publish_reading({"metric": "x", "x": 2.0})

    received: list[dict] = []
    processed = stream_service.consume(
        consumer="test-1",
        handler=received.append,
        app=app,
        block_ms=10,
        once=True,
    )
    assert processed == 2
    assert [r["x"] for r in received] == [1.0, 2.0]

    # Both entries should now be acked → no pending
    pending = redis_client.xpending(stream_service.STREAM_NAME, stream_service.CONSUMER_GROUP)
    assert pending["pending"] == 0


def test_consume_does_not_ack_when_handler_raises(app, redis_client):
    with app.app_context():
        stream_service.publish_reading({"metric": "x", "x": 99.0})

    def bad_handler(_reading):
        raise RuntimeError("boom")

    processed = stream_service.consume(
        consumer="test-2",
        handler=bad_handler,
        app=app,
        block_ms=10,
        once=True,
    )
    assert processed == 0
    pending = redis_client.xpending(stream_service.STREAM_NAME, stream_service.CONSUMER_GROUP)
    # The unacked entry is still pending against the consumer.
    assert pending["pending"] == 1


def test_consume_empty_returns_zero_with_once(app):
    """When the stream is empty and once=True, consume returns 0 quickly."""
    processed = stream_service.consume(
        consumer="test-3",
        handler=lambda _r: None,
        app=app,
        block_ms=10,
        once=True,
    )
    assert processed == 0
