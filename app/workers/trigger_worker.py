"""Trigger worker: consumes the Redis Stream populated by the MQTT bridge
and applies each reading to the in-process trigger engine.

Entry-point: `flask trigger-worker` (registered in `app/__init__.py`).
Each worker process picks a unique consumer name (env `WORKER_NAME` or the
hostname) so multiple replicas in a Compose / k8s deployment share the
group's pending list cleanly.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

from flask import Flask

from app.extensions import db
from app.services import stream as stream_service
from app.services import triggers as trigger_service

logger = logging.getLogger(__name__)


def process_reading(app: Flask, reading: dict[str, Any]) -> int:
    """Resolve line_code → line_id, evaluate triggers, commit. Returns the
    number of triggers fired (0 on failure; the exception is re-raised so
    the stream consumer leaves the entry pending for retry)."""
    with app.app_context():
        line_code = reading.get("line_code")
        if line_code and "line_id" not in reading:
            from app.models.production import ProductionLine

            line = ProductionLine.query.filter_by(code=line_code).first()
            if line is not None:
                reading["line_id"] = line.id
        try:
            fired = trigger_service.evaluate(reading)
            db.session.commit()
            return len(fired)
        except Exception:
            db.session.rollback()
            logger.exception("Trigger evaluation failed: reading=%s", reading)
            raise


def run(app: Flask, consumer: str | None = None) -> None:
    consumer = (
        consumer
        or os.environ.get("WORKER_NAME")
        or socket.gethostname()
        or "qms-worker-1"
    )
    logger.info(
        "Trigger worker starting: stream=%s group=%s consumer=%s",
        stream_service.STREAM_NAME,
        stream_service.CONSUMER_GROUP,
        consumer,
    )
    stream_service.consume(
        consumer,
        lambda reading: process_reading(app, reading),
        app=app,
    )
