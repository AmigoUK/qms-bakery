"""EMAIL responder integration: trigger fires → job appears on email queue."""

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


def _make_email_trigger(
    *,
    to,
    subject="[QMS] {trigger_code} on {scope}",
    body_text="Temperature {temperature} exceeded threshold.",
    body_html="<p>Temperature <b>{temperature}</b> exceeded threshold.</p>",
) -> Trigger:
    line = ProductionLine.query.filter_by(code="LINE_A").first()
    cfg = {"to": to, "subject": subject, "body_text": body_text}
    if body_html:
        cfg["body_html"] = body_html
    responder = Responder(
        code="EMAIL_TEST",
        name={"en": "Email test", "pl": "Email test"},
        type=ResponderType.EMAIL.value,
        config=cfg,
    )
    db.session.add(responder)
    db.session.flush()

    trigger = Trigger(
        code="OVEN1_EMAIL",
        name={"en": "Email on overheat", "pl": "Email na przegrzanie"},
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


def test_email_responder_enqueues_job(app, redis_client):
    with app.app_context():
        _disable_seeded()
        _make_email_trigger(to=["ops@example.com", "shift@example.com"])
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
        assert "EMAIL_TEST" in results
        em = results["EMAIL_TEST"]
        assert em["ok"] is True
        assert em["queued_email"] == ["ops@example.com", "shift@example.com"]
        assert "job_id" in em

        queue = queue_service.get_email_queue(app)
        assert queue.count == 1
        assert em["job_id"] in queue.get_job_ids()


def test_email_responder_interpolates_subject_and_body(app):
    with app.app_context():
        _disable_seeded()
        _make_email_trigger(to="single@example.com")
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

        queue = queue_service.get_email_queue(app)
        job_id = fired[0].responder_results["EMAIL_TEST"]["job_id"]
        job = Job.fetch(job_id, connection=queue.connection)
        # `to` string normalised to list at responder layer
        assert job.kwargs["to"] == ["single@example.com"]
        assert job.kwargs["subject"] == "[QMS] OVEN1_EMAIL on line:LINE_A"
        assert "250.0" in job.kwargs["body_text"]
        assert "<b>250.0</b>" in job.kwargs["body_html"]


def test_email_responder_without_recipient_records_failure(app):
    """An EMAIL responder configured without `to` should fail-soft."""
    with app.app_context():
        _disable_seeded()
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        responder = Responder(
            code="EMAIL_BAD",
            name={"en": "Misconfigured", "pl": "Misconfigured"},
            type=ResponderType.EMAIL.value,
            config={"subject": "x", "body_text": "x"},  # missing to
        )
        db.session.add(responder)
        db.session.flush()
        trigger = Trigger(
            code="EMAIL_BAD_TRIG",
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
        result = fired[0].responder_results["EMAIL_BAD"]
        assert result["ok"] is False
        assert "to" in result["error"].lower() or "recipient" in result["error"].lower()
