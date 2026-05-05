"""Retention sweep — UK GDPR Art. 5(1)(e)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.services import retention


def test_dry_run_counts_only_no_mutations(app):
    with app.app_context():
        summary = retention.sweep(dry_run=True, config=app.config)
    assert summary["dry_run"] is True
    # All counts present.
    for key in (
        "expired_enrolments_dropped",
        "declarations_pii_redacted",
        "trainees_redacted",
        "audit_rows_eligible_for_partition_drop",
    ):
        assert key in summary


def test_expired_enrolment_is_dropped_when_applied(app):
    from app.models import EnrolmentSource, EnrolmentStatus, TrainingEnrolment
    from app.services import training as training_service

    with app.app_context():
        trainee = training_service.create_trainee(
            phone="+447700900222",
            full_name="Retention Test",
            role_code="operator",
        )
        course = training_service.get_course_by_code("HACCP-REFRESHER")
        enrolment = training_service.enrol(
            trainee=trainee,
            course=course,
            source=EnrolmentSource.MANUAL.value,
        )
        # Backdate + mark expired so it's eligible.
        enrolment.status = EnrolmentStatus.EXPIRED.value
        enrolment.expires_at = datetime.now(timezone.utc) - timedelta(days=60)
        db.session.commit()
        enrolment_id = enrolment.id

    with app.app_context():
        before = retention.sweep(dry_run=True, config=app.config)
        assert before["expired_enrolments_dropped"] >= 1
        retention.sweep(dry_run=False, config=app.config)
        # Row gone.
        assert db.session.get(TrainingEnrolment, enrolment_id) is None


def test_recent_expired_not_dropped(app):
    """Enrolments expired within the last 30 days stay — gives ops a
    short window to investigate before destruction."""
    from app.models import EnrolmentSource, EnrolmentStatus, TrainingEnrolment
    from app.services import training as training_service

    with app.app_context():
        trainee = training_service.create_trainee(
            phone="+447700900223",
            full_name="Recent Expiry",
            role_code="operator",
        )
        course = training_service.get_course_by_code("HACCP-REFRESHER")
        enrolment = training_service.enrol(
            trainee=trainee, course=course, source=EnrolmentSource.MANUAL.value,
        )
        enrolment.status = EnrolmentStatus.EXPIRED.value
        enrolment.expires_at = datetime.now(timezone.utc) - timedelta(days=2)
        db.session.commit()
        enrolment_id = enrolment.id
        retention.sweep(dry_run=False, config=app.config)
        assert db.session.get(TrainingEnrolment, enrolment_id) is not None


def test_cli_dry_run(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["retention-sweep"])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
