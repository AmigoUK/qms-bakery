"""RQ worker entrypoint for asynchronous responder jobs.

Currently runs the webhook queue (`qms:webhooks`); add new queues here as
new async responder types are introduced (e-mail, SMS, scheduled
follow-ups, ...). The worker uses the same Redis connection the queue
service builds, so it picks up `REDIS_URL` (or the test-injected
`REDIS_CLIENT`) without further wiring.
"""

from __future__ import annotations

import logging

from flask import Flask
from rq import Worker

from app.services import queue as queue_service

logger = logging.getLogger(__name__)


def run(app: Flask) -> None:
    queue = queue_service.get_queue(app)
    logger.info(
        "RQ worker starting: queue=%s connection=%r",
        queue.name,
        queue.connection,
    )
    worker = Worker([queue], connection=queue.connection)
    worker.work()
