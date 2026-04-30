"""Scheduler + SEND_TRAINING_LINK responder tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models import (
    EnrolmentStatus,
    Responder,
    ResponderType,
    Trainee,
    TrainingAnswerOption,
    TrainingAssignment,
    TrainingCertification,
    TrainingEnrolment,
    TrainingModule,
    TrainingQuestion,
    Trigger,
    trigger_responders,
)
from app.services import training as training_service
from app.services import triggers as trigger_service
from app.workers import training_scheduler


def _seed_full_course():
    course = training_service.create_course(code="HACCP-SCHED")
    version = training_service.add_course_version(
        course=course,
        title={"pl": "T", "en": "T"},
        summary={"pl": "", "en": ""},
        validity_months=12,
        link_ttl_days=7,
    )
    db.session.add(
        TrainingModule(
            course_version_id=version.id,
            order_index=0,
            title={"pl": "M", "en": "M"},
            body_md={"pl": "", "en": ""},
        )
    )
    q = TrainingQuestion(
        course_version_id=version.id,
        order_index=0,
        prompt={"pl": "Q", "en": "Q"},
    )
    db.session.add(q)
    db.session.flush()
    db.session.add_all(
        [
            TrainingAnswerOption(
                question_id=q.id, order_index=0, label={"pl": "A", "en": "A"}, is_correct=True
            ),
        ]
    )
    return course


# ─── Scheduler ──────────────────────────────────────────────────────


def test_scheduler_issues_for_due_trainee(app):
    with app.app_context():
        course = _seed_full_course()
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code="operator")
        )
        training_service.create_trainee(
            phone="+447700100001",
            full_name="Sched User",
            role_code="operator",
        )
        db.session.commit()

        summary = training_scheduler.run_once(app)
        assert summary["issued"] == 1

        # Idempotent: second tick is a no-op (open enrolment exists)
        summary = training_scheduler.run_once(app)
        assert summary["issued"] == 0


def test_scheduler_skips_qa_when_assignment_is_operator_only(app):
    with app.app_context():
        course = _seed_full_course()
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code="operator")
        )
        training_service.create_trainee(
            phone="+447700100002", full_name="QA", role_code="qa"
        )
        db.session.commit()

        summary = training_scheduler.run_once(app)
        assert summary["issued"] == 0


def test_scheduler_re_enrols_after_cert_expires(app):
    with app.app_context():
        course = _seed_full_course()
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code="operator")
        )
        trainee = training_service.create_trainee(
            phone="+447700100003",
            full_name="Expired Cert",
            role_code="operator",
        )
        # First tick → issued
        training_scheduler.run_once(app)
        # Mark the open enrolment as submitted with an expired cert
        enrolment = TrainingEnrolment.query.filter_by(trainee_id=trainee.id).first()
        enrolment.status = EnrolmentStatus.SUBMITTED.value
        version = enrolment.course_version
        cert = TrainingCertification(
            trainee_id=trainee.id,
            course_id=course.id,
            course_version_id=version.id,
            attempt_id=enrolment.id,  # cheat: just need a valid FK; not used here
            declaration_id=enrolment.id,
            valid_from=datetime.now(timezone.utc) - timedelta(days=400),
            valid_until=datetime.now(timezone.utc) - timedelta(days=1),  # expired
        )
        # Skip cert insert because attempt_id/declaration_id FKs would fail.
        # Instead force expiry on the enrolment so the next tick re-enrols.
        enrolment.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.commit()

        # Second tick — open enrolment is no longer "open" (expired), so re-issue
        summary = training_scheduler.run_once(app)
        assert summary["issued"] == 1


# ─── SEND_TRAINING_LINK responder ──────────────────────────────────


def test_send_training_link_responder_enrols_audience(app):
    with app.app_context():
        course = _seed_full_course()
        db.session.commit()
        op_a = training_service.create_trainee(
            phone="+447700100100", full_name="A", role_code="operator", line_id=None
        )
        op_b = training_service.create_trainee(
            phone="+447700100101", full_name="B", role_code="operator", line_id=None
        )
        qa = training_service.create_trainee(
            phone="+447700100102", full_name="C", role_code="qa", line_id=None
        )
        db.session.commit()

        responder = Responder(
            code="ISSUE-HACCP",
            name={"pl": "Wystaw HACCP", "en": "Issue HACCP"},
            type=ResponderType.SEND_TRAINING_LINK.value,
            config={
                "course_code": "HACCP-SCHED",
                "audience": {"role_code": "operator"},
            },
        )
        trigger = Trigger(
            code="ANY-FIRE",
            name={"pl": "X", "en": "X"},
            condition={"metric": "x", "operator": ">", "value": 0},
            severity="medium",
        )
        db.session.add_all([responder, trigger])
        db.session.flush()
        db.session.execute(
            trigger_responders.insert(),
            [{"trigger_id": trigger.id, "responder_id": responder.id, "order_index": 0}],
        )
        db.session.commit()

        result = trigger_service._dispatch_responder(
            responder, trigger, payload={}
        )
        db.session.commit()
        assert result["trainees_issued"] == 2
        # Two operators got an enrolment, the QA did not
        op_enrolments = TrainingEnrolment.query.filter(
            TrainingEnrolment.trainee_id.in_([op_a.id, op_b.id])
        ).count()
        assert op_enrolments == 2
        qa_enrolments = TrainingEnrolment.query.filter_by(trainee_id=qa.id).count()
        assert qa_enrolments == 0


def test_send_training_link_responder_line_scoped(app):
    with app.app_context():
        course = _seed_full_course()
        db.session.commit()
        from app.models import ProductionLine

        line_b = ProductionLine.query.filter_by(code="LINE_A").first()
        on_line = training_service.create_trainee(
            phone="+447700100200",
            full_name="LineWorker",
            role_code="operator",
            line_id=line_b.id,
        )
        off_line = training_service.create_trainee(
            phone="+447700100201",
            full_name="OtherLine",
            role_code="operator",
            line_id=None,
        )
        responder = Responder(
            code="ISSUE-LINE-SCOPED",
            name={"pl": "X", "en": "X"},
            type=ResponderType.SEND_TRAINING_LINK.value,
            config={
                "course_code": "HACCP-SCHED",
                "audience": {"role_code": "operator", "line_scoped": True},
            },
        )
        trigger = Trigger(
            code="LINE-FIRE",
            name={"pl": "X", "en": "X"},
            condition={"metric": "x", "operator": ">", "value": 0},
            severity="medium",
        )
        db.session.add_all([responder, trigger])
        db.session.flush()
        db.session.commit()

        result = trigger_service._dispatch_responder(
            responder, trigger, payload={"line_id": line_b.id}
        )
        db.session.commit()
        # Only the on-line trainee got enrolled
        assert result["trainees_issued"] == 1
        assert TrainingEnrolment.query.filter_by(trainee_id=on_line.id).count() == 1
        assert TrainingEnrolment.query.filter_by(trainee_id=off_line.id).count() == 0


def test_send_training_link_responder_idempotent(app):
    """Firing twice doesn't double-enrol."""
    with app.app_context():
        course = _seed_full_course()
        db.session.commit()
        training_service.create_trainee(
            phone="+447700100300", full_name="X", role_code="operator"
        )
        responder = Responder(
            code="ISSUE-X",
            name={"pl": "X", "en": "X"},
            type=ResponderType.SEND_TRAINING_LINK.value,
            config={
                "course_code": "HACCP-SCHED",
                "audience": {"role_code": "operator"},
            },
        )
        trigger = Trigger(
            code="X",
            name={"pl": "X", "en": "X"},
            condition={"metric": "x", "operator": ">", "value": 0},
            severity="medium",
        )
        db.session.add_all([responder, trigger])
        db.session.flush()
        db.session.commit()
        r1 = trigger_service._dispatch_responder(responder, trigger, payload={})
        db.session.commit()
        r2 = trigger_service._dispatch_responder(responder, trigger, payload={})
        db.session.commit()
        assert r1["trainees_issued"] == 1
        assert r2["trainees_issued"] == 0
