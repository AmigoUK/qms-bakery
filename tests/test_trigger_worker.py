"""Trigger worker: end-to-end flow from a stream entry to a fired trigger.

Bridge → stream → worker is exercised by publishing readings via the
stream service, then invoking the worker's consume loop with `once=True`.
The seeded `OVEN1_OVERHEAT` trigger (>220°C on LINE_A) lets us verify a
ticket is created from a reading that exceeds the threshold.
"""

from __future__ import annotations

import pytest

from app.models import Ticket, TriggerExecution
from app.mqtt.bridge import enqueue_message
from app.services import stream as stream_service
from app.workers import trigger_worker


def test_enqueue_then_worker_fires_seeded_trigger(app, redis_client):
    """Bridge enqueues a 232.5°C reading; worker dequeues and fires the trigger."""
    entry_id = enqueue_message(
        app, "factory/LINE_A/oven_1/temperature", b'{"value": 232.5}'
    )
    assert entry_id is not None
    assert redis_client.xlen(stream_service.STREAM_NAME) == 1

    fired = stream_service.consume(
        consumer="worker-test",
        handler=lambda reading: trigger_worker.process_reading(app, reading),
        app=app,
        block_ms=10,
        once=True,
    )
    assert fired == 1

    with app.app_context():
        executions = TriggerExecution.query.all()
        assert len(executions) == 1
        tickets = Ticket.query.all()
        assert len(tickets) == 1


def test_worker_below_threshold_does_not_fire(app, redis_client):
    enqueue_message(app, "factory/LINE_A/oven_1/temperature", b'{"value": 180.0}')
    fired = stream_service.consume(
        consumer="worker-test",
        handler=lambda reading: trigger_worker.process_reading(app, reading),
        app=app,
        block_ms=10,
        once=True,
    )
    assert fired == 1  # the reading was processed
    with app.app_context():
        assert TriggerExecution.query.count() == 0
        assert Ticket.query.count() == 0


def test_worker_wrong_scope_does_not_fire(app, redis_client):
    enqueue_message(app, "factory/LINE_X/oven_99/temperature", b'{"value": 999.0}')
    stream_service.consume(
        consumer="worker-test",
        handler=lambda reading: trigger_worker.process_reading(app, reading),
        app=app,
        block_ms=10,
        once=True,
    )
    with app.app_context():
        assert TriggerExecution.query.count() == 0


def test_enqueue_unparseable_returns_none(app, redis_client):
    assert enqueue_message(app, "garbage", b"1") is None
    assert enqueue_message(app, "factory/LINE_A/oven_1/temperature", b"oops") is None
    # Stream stays empty for unparseable inputs.
    assert redis_client.xlen(stream_service.STREAM_NAME) == 0


def test_process_reading_reraises_on_evaluation_failure(app, monkeypatch):
    """If the trigger engine raises, process_reading re-raises so the
    consume loop leaves the stream entry pending for retry."""
    from app.services import triggers as trigger_service

    def boom(_payload):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(trigger_service, "evaluate", boom)

    with pytest.raises(RuntimeError):
        trigger_worker.process_reading(
            app, {"metric": "temperature", "temperature": 232.5, "line_code": "LINE_A"}
        )
