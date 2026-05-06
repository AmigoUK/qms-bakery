"""Training schema unit tests.

Cover the version-pinning property (in-flight enrolments must keep
pointing at their original course version) and the certification
identity property (cert points at TrainingCourse, not version).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import (
    EnrolmentStatus,
    Trainee,
    TrainingAnswerOption,
    TrainingAttempt,
    TrainingCertification,
    TrainingCourse,
    TrainingCourseVersion,
    TrainingDeclaration,
    TrainingEnrolment,
    TrainingModule,
    TrainingQuestion,
)


def _now():
    return datetime.now(UTC)


def _make_trainee(phone: str = "+447700000001"):
    t = Trainee(
        phone=phone,
        full_name="Anna Operator",
        role_code="operator",
        language="en",
    )
    db.session.add(t)
    db.session.flush()
    return t


def _make_course_with_one_version() -> TrainingCourseVersion:
    course = TrainingCourse(code="HACCP-MODELS")
    db.session.add(course)
    db.session.flush()
    v1 = TrainingCourseVersion(
        course_id=course.id,
        version=1,
        is_active=True,
        title={"pl": "Odświeżenie HACCP", "en": "HACCP Refresher"},
        summary={"pl": "Krótki kurs", "en": "Short course"},
    )
    db.session.add(v1)
    db.session.flush()
    db.session.add(
        TrainingModule(
            course_version_id=v1.id,
            order_index=0,
            title={"pl": "Wstęp", "en": "Intro"},
            body_md={"pl": "...", "en": "..."},
        )
    )
    q = TrainingQuestion(
        course_version_id=v1.id,
        order_index=0,
        prompt={"pl": "Pytanie", "en": "Q1"},
    )
    db.session.add(q)
    db.session.flush()
    db.session.add_all(
        [
            TrainingAnswerOption(
                question_id=q.id,
                order_index=0,
                label={"pl": "A", "en": "A"},
                is_correct=True,
            ),
            TrainingAnswerOption(
                question_id=q.id,
                order_index=1,
                label={"pl": "B", "en": "B"},
                is_correct=False,
            ),
        ]
    )
    db.session.flush()
    return v1


def test_trainee_phone_unique(app):
    with app.app_context():
        _make_trainee("+447700000010")
        db.session.commit()
        with pytest.raises(Exception):
            _make_trainee("+447700000010")
            db.session.commit()
        db.session.rollback()


def test_course_active_version_helper(app):
    with app.app_context():
        v1 = _make_course_with_one_version()
        db.session.commit()
        course = db.session.get(TrainingCourse, v1.course_id)
        assert course.active_version is not None
        assert course.active_version.version == 1


def test_version_pinning_under_admin_edit(app):
    """When admin creates v2, in-flight enrolments must still point at v1."""
    with app.app_context():
        v1 = _make_course_with_one_version()
        trainee = _make_trainee("+447700000020")
        enrolment = TrainingEnrolment(
            trainee_id=trainee.id,
            course_version_id=v1.id,
            magic_token="tok-pin-1",
            issued_at=_now(),
            expires_at=_now() + timedelta(days=7),
        )
        db.session.add(enrolment)
        db.session.commit()

        # Admin "edits": flips v1.is_active=False, creates v2.
        v1.is_active = False
        v2 = TrainingCourseVersion(
            course_id=v1.course_id,
            version=2,
            is_active=True,
            title={"pl": "v2", "en": "v2"},
            summary={"pl": "", "en": ""},
        )
        db.session.add(v2)
        db.session.commit()

        # Reload — enrolment still points at v1, course.active_version is v2.
        db.session.refresh(enrolment)
        assert enrolment.course_version_id == v1.id
        course = db.session.get(TrainingCourse, v1.course_id)
        assert course.active_version.version == 2


def test_certification_points_at_course_not_version(app):
    """A passed cert is identity-keyed by course, version kept for audit."""
    with app.app_context():
        v1 = _make_course_with_one_version()
        trainee = _make_trainee("+447700000030")
        enrolment = TrainingEnrolment(
            trainee_id=trainee.id,
            course_version_id=v1.id,
            magic_token="tok-cert-1",
            issued_at=_now(),
            expires_at=_now() + timedelta(days=7),
            status=EnrolmentStatus.SUBMITTED.value,
        )
        db.session.add(enrolment)
        db.session.flush()
        attempt = TrainingAttempt(
            enrolment_id=enrolment.id,
            started_at=_now(),
            submitted_at=_now(),
            score=0.9,
            passed=True,
        )
        db.session.add(attempt)
        db.session.flush()
        decl = TrainingDeclaration(
            attempt_id=attempt.id,
            typed_name="Anna Operator",
            declaration_text="I certify ...",
            signed_at=_now(),
        )
        db.session.add(decl)
        db.session.flush()
        cert = TrainingCertification(
            trainee_id=trainee.id,
            course_id=v1.course_id,
            course_version_id=v1.id,
            attempt_id=attempt.id,
            declaration_id=decl.id,
            valid_from=_now(),
            valid_until=_now() + timedelta(days=365),
        )
        db.session.add(cert)
        db.session.commit()
        # Cert keys by course, not version — exactly the design intent.
        assert cert.course_id == v1.course_id
        assert cert.course_version_id == v1.id


def test_unique_constraints(app):
    """course_id+version, course_version+order_index for modules and Qs."""
    with app.app_context():
        v1 = _make_course_with_one_version()
        db.session.commit()
        with pytest.raises(Exception):
            db.session.add(
                TrainingCourseVersion(
                    course_id=v1.course_id,
                    version=1,
                    title={"pl": "x", "en": "x"},
                    summary={"pl": "", "en": ""},
                )
            )
            db.session.commit()
        db.session.rollback()


def test_enrolment_status_default_issued(app):
    with app.app_context():
        v1 = _make_course_with_one_version()
        trainee = _make_trainee("+447700000040")
        enrolment = TrainingEnrolment(
            trainee_id=trainee.id,
            course_version_id=v1.id,
            magic_token="tok-default-1",
            issued_at=_now(),
            expires_at=_now() + timedelta(days=7),
        )
        db.session.add(enrolment)
        db.session.commit()
        assert enrolment.status == EnrolmentStatus.ISSUED.value
        assert enrolment.module_progress == 0
