"""SMS responder integration: trigger fires → job appears on sms queue."""

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


def _make_sms_trigger(*, to, body="ALERT {trigger_code}: temp={temperature}") -> Trigger:
    line = ProductionLine.query.filter_by(code="LINE_A").first()
    responder = Responder(
        code="SMS_TEST",
        name={"en": "SMS test", "pl": "SMS test"},
        type=ResponderType.SMS.value,
        config={"to": to, "body": body},
    )
    db.session.add(responder)
    db.session.flush()

    trigger = Trigger(
        code="OVEN1_SMS",
        name={"en": "SMS on overheat", "pl": "SMS na przegrzanie"},
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


def _disable_seeded():
    seeded = Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()
    if seeded:
        seeded.is_active = False
        db.session.flush()


def test_sms_responder_enqueues_job(app):
    with app.app_context():
        _disable_seeded()
        _make_sms_trigger(to=["+447700900000", "+447700900001"])
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
        assert "SMS_TEST" in results
        sms = results["SMS_TEST"]
        assert sms["ok"] is True
        assert sms["queued_sms"] == ["+447700900000", "+447700900001"]
        assert "job_id" in sms

        queue = queue_service.get_sms_queue(app)
        assert queue.count == 1
        assert sms["job_id"] in queue.get_job_ids()


def test_sms_responder_interpolates_body(app):
    with app.app_context():
        _disable_seeded()
        _make_sms_trigger(to="+447700900000")
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

        from rq.job import Job

        queue = queue_service.get_sms_queue(app)
        job_id = fired[0].responder_results["SMS_TEST"]["job_id"]
        job = Job.fetch(job_id, connection=queue.connection)
        assert job.kwargs["to"] == ["+447700900000"]
        assert "OVEN1_SMS" in job.kwargs["body"]
        assert "250.0" in job.kwargs["body"]


def test_sms_responder_without_recipient_records_failure(app):
    """Misconfigured SMS responder (no `to`) must fail-soft like email."""
    with app.app_context():
        _disable_seeded()
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        responder = Responder(
            code="SMS_BAD",
            name={"en": "Misconfigured", "pl": "Misconfigured"},
            type=ResponderType.SMS.value,
            config={"body": "x"},  # missing to
        )
        db.session.add(responder)
        db.session.flush()
        trigger = Trigger(
            code="SMS_BAD_TRIG",
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
        result = fired[0].responder_results["SMS_BAD"]
        assert result["ok"] is False
        assert "to" in result["error"].lower() or "recipient" in result["error"].lower()
