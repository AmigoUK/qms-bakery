"""`flask training-issue-link` CLI smoke command."""

from __future__ import annotations

from app.extensions import db
from app.models import Trainee, TrainingEnrolment


def test_cli_dry_run_creates_trainee_and_enrolment_no_sms(app):
    """--dry-run path: trainee + enrolment land in the DB, SMS is not
    queued. Useful for validating URL shape before burning ClickSend credit."""
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "training-issue-link",
            "--phone", "+447700099001",
            "--course", "HACCP-REFRESHER",
            "--name", "CLI User",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created trainee" in result.output
    assert "DRY RUN" in result.output
    assert "/training/take/" in result.output

    with app.app_context():
        t = Trainee.query.filter_by(phone="+447700099001").first()
        assert t is not None
        assert TrainingEnrolment.query.filter_by(trainee_id=t.id).count() == 1


def test_cli_send_path_queues_real_enrolment(app):
    """--send (default) path: also queues an SMS via the existing
    ClickSend pipeline. The fakeredis-backed RQ doesn't actually
    deliver; the job sits on the queue."""
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "training-issue-link",
            "--phone", "+447700099002",
            "--name", "Send User",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Enrolment" in result.output
    assert "/training/take/" in result.output

    with app.app_context():
        t = Trainee.query.filter_by(phone="+447700099002").first()
        assert t is not None
        assert TrainingEnrolment.query.filter_by(trainee_id=t.id).count() == 1


def test_cli_unknown_course_errors(app):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "training-issue-link",
            "--phone", "+447700099003",
            "--course", "DOES-NOT-EXIST",
        ],
    )
    assert result.exit_code != 0
    assert "not found" in result.output


def test_cli_reuses_existing_trainee(app):
    """Calling twice with the same phone reuses the trainee row."""
    runner = app.test_cli_runner()
    runner.invoke(
        args=[
            "training-issue-link",
            "--phone", "+447700099004",
            "--name", "Reused",
            "--dry-run",
        ],
    )
    result = runner.invoke(
        args=[
            "training-issue-link",
            "--phone", "+447700099004",
            "--dry-run",
        ],
    )
    assert "Reusing trainee" in result.output
    with app.app_context():
        # Same trainee row, two enrolments.
        ts = Trainee.query.filter_by(phone="+447700099004").all()
        assert len(ts) == 1
        assert TrainingEnrolment.query.filter_by(trainee_id=ts[0].id).count() == 2
