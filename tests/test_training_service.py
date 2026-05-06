"""Domain service tests: enrol → take → score → declare → certify
plus recurrence detection for the scheduler.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import (
    EnrolmentStatus,
    TrainingAnswerOption,
    TrainingAssignment,
    TrainingModule,
    TrainingQuestion,
)
from app.services import training as t


def _now():
    return datetime.now(UTC)


def _is_future(dt: datetime) -> bool:
    """SQLite drops tzinfo on read-back; compare in epoch seconds to side-step.
    PG preserves it correctly; this helper is a test-only quirk."""
    if dt.tzinfo is None:
        return dt.timestamp() > datetime.now().timestamp()
    return dt > _now()


def _seed_course() -> tuple:
    """Helper: create a course with one version and one Q (A correct, B wrong)."""
    course = t.create_course(code="HACCP-TEST")
    version = t.add_course_version(
        course=course,
        title={"pl": "Test", "en": "Test"},
        summary={"pl": "", "en": ""},
        pass_threshold=0.5,  # 1/1 needed for pass
        validity_months=12,
        link_ttl_days=7,
    )
    db.session.add(
        TrainingModule(
            course_version_id=version.id,
            order_index=0,
            title={"pl": "M1", "en": "M1"},
            body_md={"pl": "...", "en": "..."},
        )
    )
    q = TrainingQuestion(
        course_version_id=version.id,
        order_index=0,
        prompt={"pl": "Q?", "en": "Q?"},
    )
    db.session.add(q)
    db.session.flush()
    db.session.add_all(
        [
            TrainingAnswerOption(
                question_id=q.id, order_index=0, label={"pl": "A", "en": "A"}, is_correct=True
            ),
            TrainingAnswerOption(
                question_id=q.id, order_index=1, label={"pl": "B", "en": "B"}, is_correct=False
            ),
        ]
    )
    db.session.flush()
    return course, version, q


def test_create_trainee_dedupes(app):
    with app.app_context():
        t.create_trainee(phone="+447700000100", full_name="A", role_code="operator")
        db.session.commit()
        with pytest.raises(t.TrainingError, match="already registered"):
            t.create_trainee(phone="+447700000100", full_name="B", role_code="operator")


def test_enrol_issues_token_and_enqueues_sms(app):
    with app.app_context():
        course, version, _ = _seed_course()
        trainee = t.create_trainee(
            phone="+447700000200", full_name="X", role_code="operator"
        )
        enrolment = t.enrol(
            trainee=trainee, course=course, base_url="https://qms.test"
        )
        db.session.commit()
        assert enrolment.magic_token.count(".") == 2
        assert _is_future(enrolment.expires_at)
        assert enrolment.status == EnrolmentStatus.ISSUED.value
        looked_up = t.get_enrolment_by_token(enrolment.magic_token)
        assert looked_up is not None
        assert looked_up.id == enrolment.id


def test_enrol_requires_active_version(app):
    with app.app_context():
        course = t.create_course(code="EMPTY")
        trainee = t.create_trainee(
            phone="+447700000300", full_name="X", role_code="operator"
        )
        with pytest.raises(t.TrainingError, match="no active version"):
            t.enrol(trainee=trainee, course=course)


def test_full_pass_flow(app):
    with app.app_context():
        course, version, q = _seed_course()
        trainee = t.create_trainee(
            phone="+447700000400", full_name="Pass User", role_code="operator"
        )
        enrolment = t.enrol(trainee=trainee, course=course, base_url="https://qms.test")
        attempt = t.start_attempt(enrolment)
        assert enrolment.status == EnrolmentStatus.STARTED.value

        correct_id = next(o.id for o in q.options if o.is_correct)
        attempt = t.submit_attempt(enrolment, {q.id: [correct_id]})
        assert attempt.passed is True
        assert attempt.score == 1.0

        decl = t.record_declaration(
            attempt=attempt,
            typed_name="Pass User",
            declaration_text="I certify ...",
            signature_png=b"\x89PNG\r\n",
            ip_address="10.0.0.1",
            user_agent="Mozilla/5.0",
        )
        db.session.commit()
        cert = t.latest_certification(trainee.id, course.id)
        assert cert is not None
        assert cert.attempt_id == attempt.id
        assert cert.declaration_id == decl.id
        assert _is_future(cert.valid_until)


def test_full_fail_flow_no_certification(app):
    with app.app_context():
        course, version, q = _seed_course()
        trainee = t.create_trainee(
            phone="+447700000500", full_name="Fail User", role_code="operator"
        )
        enrolment = t.enrol(trainee=trainee, course=course, base_url="https://qms.test")
        wrong_id = next(o.id for o in q.options if not o.is_correct)
        attempt = t.submit_attempt(enrolment, {q.id: [wrong_id]})
        assert attempt.passed is False
        t.record_declaration(
            attempt=attempt,
            typed_name="Fail User",
            declaration_text="I certify ...",
        )
        db.session.commit()
        # No cert issued — the trainee failed.
        assert t.latest_certification(trainee.id, course.id) is None


def test_double_submit_rejected(app):
    with app.app_context():
        course, version, q = _seed_course()
        trainee = t.create_trainee(
            phone="+447700000600", full_name="X", role_code="operator"
        )
        enrolment = t.enrol(trainee=trainee, course=course, base_url="https://qms.test")
        correct_id = next(o.id for o in q.options if o.is_correct)
        t.submit_attempt(enrolment, {q.id: [correct_id]})
        with pytest.raises(t.TrainingError, match="already submitted"):
            t.submit_attempt(enrolment, {q.id: [correct_id]})


def test_double_declaration_rejected(app):
    with app.app_context():
        course, version, q = _seed_course()
        trainee = t.create_trainee(
            phone="+447700000700", full_name="X", role_code="operator"
        )
        enrolment = t.enrol(trainee=trainee, course=course, base_url="https://qms.test")
        correct_id = next(o.id for o in q.options if o.is_correct)
        attempt = t.submit_attempt(enrolment, {q.id: [correct_id]})
        t.record_declaration(
            attempt=attempt, typed_name="X", declaration_text="ok"
        )
        with pytest.raises(t.TrainingError, match="already recorded"):
            t.record_declaration(
                attempt=attempt, typed_name="X", declaration_text="ok"
            )


def test_get_enrolment_by_token_rejects_expired(app):
    with app.app_context():
        course, version, _ = _seed_course()
        trainee = t.create_trainee(
            phone="+447700000800", full_name="X", role_code="operator"
        )
        enrolment = t.enrol(trainee=trainee, course=course, base_url="https://qms.test")
        # Expire it manually.
        enrolment.expires_at = _now() - timedelta(seconds=1)
        db.session.flush()
        assert t.get_enrolment_by_token(enrolment.magic_token) is None


def test_recurrence_due_when_no_cert(app):
    with app.app_context():
        course, _, _ = _seed_course()
        trainee = t.create_trainee(
            phone="+447700000900", full_name="X", role_code="operator"
        )
        assert t.is_due_for_recert(trainee, course) is True


def test_recurrence_not_due_when_open_enrolment(app):
    with app.app_context():
        course, _, _ = _seed_course()
        trainee = t.create_trainee(
            phone="+447700001000", full_name="X", role_code="operator"
        )
        t.enrol(trainee=trainee, course=course, base_url="https://qms.test")
        assert t.is_due_for_recert(trainee, course) is False


def test_recurrence_due_when_cert_expired(app):
    with app.app_context():
        course, version, q = _seed_course()
        trainee = t.create_trainee(
            phone="+447700001100", full_name="X", role_code="operator"
        )
        enrolment = t.enrol(trainee=trainee, course=course, base_url="https://qms.test")
        correct_id = next(o.id for o in q.options if o.is_correct)
        attempt = t.submit_attempt(enrolment, {q.id: [correct_id]})
        t.record_declaration(
            attempt=attempt, typed_name="X", declaration_text="ok"
        )
        cert = t.latest_certification(trainee.id, course.id)
        cert.valid_until = _now() - timedelta(days=1)
        db.session.flush()
        assert t.is_due_for_recert(trainee, course) is True


def test_trainees_due_filtered_by_assignment(app):
    with app.app_context():
        course, _, _ = _seed_course()
        # Assignment scoped to operators only
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code="operator")
        )
        db.session.flush()
        op = t.create_trainee(
            phone="+447700001200", full_name="Op", role_code="operator"
        )
        qa = t.create_trainee(phone="+447700001201", full_name="QA", role_code="qa")
        db.session.commit()
        due = list(t.trainees_due_for_course(course))
        ids = {x.id for x in due}
        assert op.id in ids
        assert qa.id not in ids
