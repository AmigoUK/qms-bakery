"""WEBHOOK responder integration: trigger fires → job appears on queue.

Uses the fakeredis-backed queue so we can verify enqueue without running
an actual RQ worker.
"""

from __future__ import annotations

from app.extensions import db
from app.models.production import ProductionLine
from app.models.triggers import (
    Responder,
    ResponderType,
    Trigger,
    trigger_responders,
)
from app.services import queue as queue_service
from app.services import triggers as trigger_service


def _make_webhook_trigger(url: str, secret: str | None = None) -> Trigger:
    line = ProductionLine.query.filter_by(code="LINE_A").first()
    config = {"url": url}
    if secret:
        config["secret"] = secret
    responder = Responder(
        code="WEBHOOK_TEST",
        name={"en": "Webhook test", "pl": "Webhook test"},
        type=ResponderType.WEBHOOK.value,
        config=config,
    )
    db.session.add(responder)
    db.session.flush()

    trigger = Trigger(
        code="OVEN1_WEBHOOK",
        name={"en": "Webhook on overheat", "pl": "Webhook na przegrzanie"},
        scope=f"line:{line.code}",
        condition={"metric": "temperature", "operator": ">", "value": 220},
        severity="high",
        is_active=True,
    )
    db.session.add(trigger)
    db.session.flush()

    db.session.execute(
        trigger_responders.insert(),
        [{"trigger_id": trigger.id, "responder_id": responder.id, "order_index": 0}],
    )
    db.session.flush()
    return trigger


def test_webhook_responder_enqueues_job(app, redis_client):
    with app.app_context():
        # Disable seeded trigger to isolate counts
        seeded = Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()
        seeded.is_active = False
        db.session.flush()

        trigger = _make_webhook_trigger("https://hooks.example.test/incident")
        line = ProductionLine.query.filter_by(code="LINE_A").first()

        fired = trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 232.5,
                "scope": "line:LINE_A",
                "line_id": line.id,
            }
        )
        db.session.commit()

        assert len(fired) == 1
        results = fired[0].responder_results or {}
        assert "WEBHOOK_TEST" in results
        wb = results["WEBHOOK_TEST"]
        assert wb["ok"] is True
        assert wb["queued_webhook"] == "https://hooks.example.test/incident"
        assert "job_id" in wb

        # Job is actually on the queue, not just claimed to be
        queue = queue_service.get_queue(app)
        assert queue.count == 1
        # Use ids() to locate the enqueued job
        job_ids = queue.get_job_ids()
        assert wb["job_id"] in job_ids


def test_webhook_responder_passes_url_and_secret_to_job(app):
    with app.app_context():
        seeded = Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()
        seeded.is_active = False
        db.session.flush()

        _make_webhook_trigger(
            "https://hooks.example.test/secure", secret="shh-secret"
        )
        line = ProductionLine.query.filter_by(code="LINE_A").first()

        fired = trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 250.0,
                "scope": "line:LINE_A",
                "line_id": line.id,
            }
        )
        db.session.commit()

        queue = queue_service.get_queue(app)
        from rq.job import Job

        job_id = fired[0].responder_results["WEBHOOK_TEST"]["job_id"]
        job = Job.fetch(job_id, connection=queue.connection)
        assert job.kwargs["url"] == "https://hooks.example.test/secure"
        assert job.kwargs["secret"] == "shh-secret"
        # Inner payload contains trigger metadata
        assert job.kwargs["payload"]["trigger_code"] == "OVEN1_WEBHOOK"
        assert job.kwargs["payload"]["severity"] == "high"


def test_webhook_responder_without_url_raises_in_responder(app):
    """A WEBHOOK responder configured without `url` should fail-soft: the
    responder records the failure but the trigger-execution row is still
    written so audit isn't lost."""
    with app.app_context():
        seeded = Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()
        seeded.is_active = False
        db.session.flush()

        line = ProductionLine.query.filter_by(code="LINE_A").first()
        responder = Responder(
            code="WEBHOOK_BAD",
            name={"en": "Misconfigured", "pl": "Misconfigured"},
            type=ResponderType.WEBHOOK.value,
            config={},  # missing url
        )
        db.session.add(responder)
        db.session.flush()
        trigger = Trigger(
            code="WEBHOOK_BAD_TRIG",
            name={"en": "x", "pl": "x"},
            scope="line:LINE_A",
            condition={"metric": "temperature", "operator": ">", "value": 220},
            severity="medium",
            is_active=True,
        )
        db.session.add(trigger)
        db.session.flush()
        db.session.execute(
            trigger_responders.insert(),
            [
                {
                    "trigger_id": trigger.id,
                    "responder_id": responder.id,
                    "order_index": 0,
                }
            ],
        )
        db.session.flush()

        fired = trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 232.5,
                "scope": "line:LINE_A",
                "line_id": line.id,
            }
        )
        db.session.commit()
        result = fired[0].responder_results["WEBHOOK_BAD"]
        assert result["ok"] is False
        assert "url" in result["error"].lower()


def test_enqueue_webhook_carries_retry_policy(app):
    """The enqueued job must have the 3/9/27 minute retry intervals
    declared so RQ schedules the right backoff."""
    with app.app_context():
        job = queue_service.enqueue_webhook(
            "https://hooks.example.test/retry-test",
            payload={"x": 1},
        )
        assert job.retries_left == 3
        assert job.retry_intervals == [180, 540, 1620]
