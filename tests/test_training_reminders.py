"""Reminder dispatch — kind selection, idempotency, channel-aware delivery.

Three reminder kinds:
  3d    — 3 days after issue (only when not on the final day)
  7d    — 7 days after issue (skipped when it would land on final day)
  final — on the day the link expires; carries the strongest copy
          ("you won't be cleared for work today without finishing")

Each kind is gated by its own `reminder_*_sent_at` timestamp on the
enrolment so the same kind never fires twice. SUBMITTED enrolments
are excluded — there's nothing to remind.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from app.extensions import db
from app.models import (
    EnrolmentStatus,
    TrainingAnswerOption,
    TrainingCourse,
    TrainingEnrolment,
    TrainingModule,
    TrainingQuestion,
)
from app.services import training as t


def _seed_course(code: str = "REM-A", *, link_ttl_days: int = 14) -> TrainingCourse:
    course = t.create_course(code=code)
    version = t.add_course_version(
        course=course,
        title={"pl": code, "en": code},
        summary={"pl": "", "en": ""},
        pass_threshold=0.5,
        link_ttl_days=link_ttl_days,
    )
    db.session.add(
        TrainingModule(
            course_version_id=version.id, order_index=0,
            title={"pl": "M", "en": "M"},
            body_md={"pl": "<p>m</p>", "en": "<p>m</p>"},
        )
    )
    q = TrainingQuestion(
        course_version_id=version.id, order_index=0, prompt={"pl": "?", "en": "?"}
    )
    db.session.add(q)
    db.session.flush()
    db.session.add_all([
        TrainingAnswerOption(
            question_id=q.id, order_index=0, label={"pl": "A", "en": "A"}, is_correct=True
        ),
        TrainingAnswerOption(
            question_id=q.id, order_index=1, label={"pl": "B", "en": "B"}, is_correct=False
        ),
    ])
    db.session.flush()
    return course


def _enrol(trainee, course):
    """Issue a magic-link without firing real SMS/email."""
    with (
        mock.patch("app.services.training.enqueue_sms"),
        mock.patch("app.services.training.enqueue_email"),
    ):
        return t.enrol(trainee=trainee, course=course, base_url="https://test")


def _make_trainee(phone: str, emp: str, *, channel: str = "sms"):
    return t.create_trainee(
        employee_number=emp, phone=phone, email=f"{emp.lower()}@example.com",
        full_name=f"Worker {emp}", role_code="operator",
        notification_channel=channel,
    )


# ─── kind selection ─────────────────────────────────────────────────


def test_classify_returns_none_when_too_early(app):
    """An enrolment created right now has no reminder due."""
    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000001", "REM-1")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        db.session.commit()

        kind = t._classify_reminder(enrolment, now=t._now())
    assert kind is None


def test_classify_returns_3d_after_three_days(app):
    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000002", "REM-2")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        db.session.commit()
        # Pretend 3.5 days have passed.
        future = enrolment.issued_at + timedelta(days=3, hours=12)

        kind = t._classify_reminder(enrolment, now=future)
    assert kind == t.REMINDER_KIND_3D


def test_classify_returns_7d_after_seven_days_with_long_ttl(app):
    """With a 14-day TTL, day 7 is comfortably before expiry."""
    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000003", "REM-3")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        # Already mark 3d as sent so we don't pick it up again.
        enrolment.reminder_3d_sent_at = enrolment.issued_at + timedelta(days=3)
        db.session.commit()
        future = enrolment.issued_at + timedelta(days=7, hours=2)

        kind = t._classify_reminder(enrolment, now=future)
    assert kind == t.REMINDER_KIND_7D


def test_classify_returns_final_on_expiry_day(app):
    """When `now.date() == expires_at.date()`, the final reminder
    wins regardless of how long the TTL was."""
    with app.app_context():
        course = _seed_course(link_ttl_days=7)
        trainee = _make_trainee("+447740000004", "REM-4")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        db.session.commit()
        # Same calendar day as expires_at.
        future = enrolment.expires_at.replace(hour=9, minute=0)

        kind = t._classify_reminder(enrolment, now=future)
    assert kind == t.REMINDER_KIND_FINAL


def test_classify_collapses_7d_into_final_when_ttl_short(app):
    """TTL=7d → reminder #2 (after 7d) WOULD land on the same day as
    final. The classifier picks `final` only — never 7d on that
    boundary day."""
    with app.app_context():
        course = _seed_course(link_ttl_days=7)
        trainee = _make_trainee("+447740000005", "REM-5")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        enrolment.reminder_3d_sent_at = enrolment.issued_at + timedelta(days=3)
        db.session.commit()
        future = enrolment.expires_at.replace(hour=9, minute=0)

        kind = t._classify_reminder(enrolment, now=future)
    assert kind == t.REMINDER_KIND_FINAL


def test_classify_skips_submitted_enrolment(app):
    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000006", "REM-6")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        enrolment.status = EnrolmentStatus.SUBMITTED.value
        db.session.commit()
        future = enrolment.issued_at + timedelta(days=5)

        kind = t._classify_reminder(enrolment, now=future)
    assert kind is None


def test_classify_skips_expired_enrolment(app):
    """Past-expiry tokens are out of scope; let the EXPIRED sweep
    handle them."""
    with app.app_context():
        course = _seed_course(link_ttl_days=7)
        trainee = _make_trainee("+447740000007", "REM-7")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        db.session.commit()
        future = enrolment.expires_at + timedelta(days=1)

        kind = t._classify_reminder(enrolment, now=future)
    assert kind is None


def test_classify_returns_none_when_already_sent(app):
    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000008", "REM-8")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        db.session.commit()
        future = enrolment.issued_at + timedelta(days=4)

        # Mark 3d reminder as already sent.
        enrolment.reminder_3d_sent_at = future
        db.session.commit()

        kind = t._classify_reminder(enrolment, now=future)
    assert kind is None  # No 3d (already sent), and 7d not yet due.


# ─── send_pending_reminders integration ────────────────────────────


def test_send_pending_marks_3d_and_calls_sms(app):
    """A trainee 4 days post-issue with channel=sms should get one
    SMS call and the 3d timestamp populated."""
    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000010", "REM-S1", channel="sms")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        # Backdate so the classifier sees us 4 days in.
        enrolment.issued_at = t._now() - timedelta(days=4)
        db.session.commit()
        eid = enrolment.id

    with app.app_context():
        with (
            mock.patch("app.services.training.enqueue_sms") as msms,
            mock.patch("app.services.training.enqueue_email") as memail,
        ):
            counts = t.send_pending_reminders(base_url="https://test")
            db.session.commit()

    assert counts["3d"] == 1
    assert msms.call_count == 1
    assert memail.call_count == 0
    with app.app_context():
        e = db.session.get(TrainingEnrolment, eid)
        assert e.reminder_3d_sent_at is not None


def test_send_pending_calls_email_when_channel_email(app):
    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000011", "REM-S2", channel="email")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        enrolment.issued_at = t._now() - timedelta(days=4)
        db.session.commit()

    with app.app_context():
        with (
            mock.patch("app.services.training.enqueue_sms") as msms,
            mock.patch("app.services.training.enqueue_email") as memail,
        ):
            counts = t.send_pending_reminders(base_url="https://test")
            db.session.commit()
    assert counts["3d"] == 1
    assert msms.call_count == 0
    assert memail.call_count == 1


def test_send_pending_idempotent_within_one_window(app):
    """Two consecutive ticks within the same 3-day window must not
    re-send the same kind."""
    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000012", "REM-S3")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        enrolment.issued_at = t._now() - timedelta(days=4)
        db.session.commit()

    with app.app_context():
        with (
            mock.patch("app.services.training.enqueue_sms") as msms,
            mock.patch("app.services.training.enqueue_email"),
        ):
            counts1 = t.send_pending_reminders(base_url="https://test")
            db.session.commit()
            counts2 = t.send_pending_reminders(base_url="https://test")
            db.session.commit()
    assert counts1["3d"] == 1
    assert counts2["3d"] == 0  # no double-send
    assert msms.call_count == 1


def test_send_pending_skips_submitted(app):
    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000013", "REM-S4")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        enrolment.issued_at = t._now() - timedelta(days=4)
        enrolment.status = EnrolmentStatus.SUBMITTED.value
        db.session.commit()

    with app.app_context():
        with (
            mock.patch("app.services.training.enqueue_sms") as msms,
            mock.patch("app.services.training.enqueue_email"),
        ):
            counts = t.send_pending_reminders(base_url="https://test")
            db.session.commit()
    assert counts["3d"] == 0
    assert msms.call_count == 0


def test_send_pending_final_carries_strong_copy(app):
    """Final reminder body must reference the can-not-be-cleared
    consequence (Polish copy used for Polish trainee)."""
    with app.app_context():
        course = _seed_course(link_ttl_days=7)
        trainee = _make_trainee("+447740000014", "REM-S5", channel="email")
        trainee.language = "pl"
        db.session.commit()
        enrolment = _enrol(trainee, course)
        # Force "today is the expiry day" by backdating issued_at.
        enrolment.issued_at = t._now() - timedelta(days=7)
        enrolment.expires_at = t._now() + timedelta(hours=2)
        db.session.commit()

    captured = {}

    def _capture_email(*, to, subject, body_text, body_html=None):
        captured["subject"] = subject
        captured["body"] = body_text
        return mock.MagicMock()

    with app.app_context():
        with (
            mock.patch("app.services.training.enqueue_sms"),
            mock.patch(
                "app.services.training.enqueue_email", side_effect=_capture_email
            ),
        ):
            counts = t.send_pending_reminders(base_url="https://test")
            db.session.commit()

    assert counts["final"] == 1
    assert "DZIŚ" in captured["body"] or "DZIS" in captured["body"]
    assert "dopuszczony" in captured["body"].lower()


def test_audit_records_reminder_send(app):
    from app.audit_actions import AuditAction
    from app.models import AuditLog

    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000015", "REM-S6")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        enrolment.issued_at = t._now() - timedelta(days=4)
        db.session.commit()

    with app.app_context():
        before = AuditLog.query.filter_by(
            action=AuditAction.TRAINING_REMINDER_SENT
        ).count()
        with (
            mock.patch("app.services.training.enqueue_sms"),
            mock.patch("app.services.training.enqueue_email"),
        ):
            t.send_pending_reminders(base_url="https://test")
            db.session.commit()
        after = AuditLog.query.filter_by(
            action=AuditAction.TRAINING_REMINDER_SENT
        ).count()
    assert after == before + 1


# ─── scheduler integration ──────────────────────────────────────────


def test_scheduler_run_once_includes_reminder_counts(app):
    from app.workers.training_scheduler import run_once

    with app.app_context():
        course = _seed_course(link_ttl_days=14)
        trainee = _make_trainee("+447740000020", "REM-SC1")
        db.session.commit()
        enrolment = _enrol(trainee, course)
        enrolment.issued_at = t._now() - timedelta(days=4)
        db.session.commit()

    with (
        mock.patch("app.services.training.enqueue_sms"),
        mock.patch("app.services.training.enqueue_email"),
    ):
        summary = run_once(app)

    assert "reminders" in summary
    assert summary["reminders"]["3d"] == 1
