"""Channel-aware magic-link delivery — SMS / Email / both.

The training service routes the magic-link via the trainee's
`notification_channel`:

- sms   → enqueue_sms only
- email → enqueue_email only (fallback to SMS + warning if no email)
- both  → enqueue both (or SMS only when email is missing)

These tests rely on the fact that the dev fixture queues run in-mode
through fakeredis, so we monkeypatch the enqueue helpers to a
counter and assert which lanes fired without touching real RQ.
"""

from __future__ import annotations

from unittest import mock

from app.extensions import db
from app.models import (
    NotificationChannel,
    TrainingAnswerOption,
    TrainingModule,
    TrainingQuestion,
)
from app.services import training as t


def _seed_course(code: str = "CHAN-CHK") -> object:
    course = t.create_course(code=code)
    version = t.add_course_version(
        course=course,
        title={"pl": code, "en": code},
        summary={"pl": "", "en": ""},
        pass_threshold=0.5,
        link_ttl_days=7,
    )
    db.session.add(
        TrainingModule(
            course_version_id=version.id,
            order_index=0,
            title={"pl": "M", "en": "M"},
            body_md={"pl": "<p>m</p>", "en": "<p>m</p>"},
        )
    )
    q = TrainingQuestion(
        course_version_id=version.id, order_index=0, prompt={"pl": "?", "en": "?"}
    )
    db.session.add(q)
    db.session.flush()
    db.session.add_all(
        [
            TrainingAnswerOption(
                question_id=q.id, order_index=0,
                label={"pl": "A", "en": "A"}, is_correct=True,
            ),
            TrainingAnswerOption(
                question_id=q.id, order_index=1,
                label={"pl": "B", "en": "B"}, is_correct=False,
            ),
        ]
    )
    db.session.flush()
    return course


def _patched_enrol(trainee, course):
    """Patch both enqueue paths and capture invocation counts.
    Returns (sms_count, email_count, audit_diff)."""
    with (
        mock.patch("app.services.training.enqueue_sms") as msms,
        mock.patch("app.services.training.enqueue_email") as memail,
    ):
        enrolment = t.enrol(trainee=trainee, course=course, base_url="https://test")
        db.session.commit()
        return msms.call_count, memail.call_count, enrolment


def test_sms_only_channel_queues_sms(app):
    with app.app_context():
        course = _seed_course()
        trainee = t.create_trainee(
            phone="+447700700001",
            email="x@example.com",
            full_name="SMS Only",
            role_code="operator",
            notification_channel=NotificationChannel.SMS.value,
        )
        db.session.commit()

        sms, email, _ = _patched_enrol(trainee, course)
    assert sms == 1
    assert email == 0


def test_email_only_channel_queues_email(app):
    with app.app_context():
        course = _seed_course()
        trainee = t.create_trainee(
            phone="+447700700002",
            email="y@example.com",
            full_name="Email Only",
            role_code="operator",
            notification_channel=NotificationChannel.EMAIL.value,
        )
        db.session.commit()

        sms, email, _ = _patched_enrol(trainee, course)
    assert sms == 0
    assert email == 1


def test_both_channel_queues_both(app):
    with app.app_context():
        course = _seed_course()
        trainee = t.create_trainee(
            phone="+447700700003",
            email="z@example.com",
            full_name="Both",
            role_code="operator",
            notification_channel=NotificationChannel.BOTH.value,
        )
        db.session.commit()

        sms, email, _ = _patched_enrol(trainee, course)
    assert sms == 1
    assert email == 1


def test_email_channel_falls_back_to_sms_when_no_email(app, caplog):
    """channel=email but no email on file → falls back to SMS + warns."""
    with app.app_context():
        course = _seed_course()
        trainee = t.create_trainee(
            phone="+447700700004",
            email=None,
            full_name="No Email",
            role_code="operator",
            notification_channel=NotificationChannel.EMAIL.value,
        )
        db.session.commit()

        with caplog.at_level("WARNING"):
            sms, email, _ = _patched_enrol(trainee, course)

    assert sms == 1, "SMS should fire as fallback"
    assert email == 0, "Email should NOT fire when address is missing"
    assert any("no email on file" in rec.message for rec in caplog.records), \
        "expected a warning about missing email"


def test_both_channel_with_no_email_falls_back_to_sms_only(app):
    with app.app_context():
        course = _seed_course()
        trainee = t.create_trainee(
            phone="+447700700005",
            email=None,
            full_name="Both no email",
            role_code="operator",
            notification_channel=NotificationChannel.BOTH.value,
        )
        db.session.commit()

        sms, email, _ = _patched_enrol(trainee, course)
    assert sms == 1
    assert email == 0


def test_email_failure_is_best_effort(app, caplog):
    """An exception during enqueue_email must not 500 the enrolment."""
    with app.app_context():
        course = _seed_course()
        trainee = t.create_trainee(
            phone="+447700700006",
            email="z@example.com",
            full_name="Email Boom",
            role_code="operator",
            notification_channel=NotificationChannel.EMAIL.value,
        )
        db.session.commit()

        with (
            mock.patch("app.services.training.enqueue_sms"),
            mock.patch(
                "app.services.training.enqueue_email",
                side_effect=RuntimeError("redis down"),
            ),
            caplog.at_level("WARNING"),
        ):
            enrolment = t.enrol(trainee=trainee, course=course, base_url="https://test")
            db.session.commit()

        assert enrolment.id is not None  # row persisted
        assert any("email enqueue failed" in rec.message for rec in caplog.records)
