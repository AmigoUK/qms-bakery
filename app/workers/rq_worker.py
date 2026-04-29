"""RQ worker entrypoint for asynchronous responder jobs.

Subscribes to all async responder queues:
  * `qms:webhooks` — outbound HTTP POSTs
  * `qms:emails`   — outbound SMTP sends
  * `qms:sms`      — outbound ClickSend SMS sends

All queues share the same binary Redis connection. Add a new queue here
when introducing another async responder type.
"""

from __future__ import annotations

import logging

from flask import Flask
from rq import Worker

from app.services import queue as queue_service

logger = logging.getLogger(__name__)


def run(app: Flask) -> None:
    webhook_q = queue_service.get_queue(app)
    email_q = queue_service.get_email_queue(app)
    sms_q = queue_service.get_sms_queue(app)
    queues = [webhook_q, email_q, sms_q]
    logger.info(
        "RQ worker starting: queues=%s connection=%r",
        [q.name for q in queues],
        webhook_q.connection,
    )
    worker = Worker(queues, connection=webhook_q.connection)
    worker.work()
