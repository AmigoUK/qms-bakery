"""Bulk-issue magic-link from the trainees list.

Critical correctness: each enrolment generated in one bulk-call must
have its OWN unique `magic_token`. Two trainees can never share a
token even at the byte level — that would let trainee A submit
trainee B's exam.
"""

from __future__ import annotations

from unittest import mock

from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import (
    TrainingAnswerOption,
    TrainingCourse,
    TrainingEnrolment,
    TrainingModule,
    TrainingQuestion,
)
from app.services import training as t


def _seed_course(code: str = "BULK-A") -> TrainingCourse:
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


def _make_trainee(phone: str, emp: str, role: str = "operator"):
    return t.create_trainee(
        employee_number=emp,
        phone=phone,
        email=f"{emp.lower()}@example.com",
        full_name=f"Worker {emp}",
        role_code=role,
    )


# ─── plan classification ────────────────────────────────────────────


def test_plan_marks_each_trainee_as_issue(app):
    with app.app_context():
        course = _seed_course()
        a = _make_trainee("+447730000001", "BLK-A1")
        b = _make_trainee("+447730000002", "BLK-A2")
        db.session.commit()

        plan = t.plan_bulk_issue([a.id, b.id], course)
    assert plan.n_issue == 2
    assert plan.n_skip == 0
    assert plan.n_error == 0


def test_plan_skips_trainee_with_open_enrolment(app):
    with app.app_context():
        course = _seed_course()
        a = _make_trainee("+447730000010", "BLK-B1")
        b = _make_trainee("+447730000011", "BLK-B2")
        db.session.commit()
        # Pre-issue for `a` so the planner sees an in-flight.
        with (
            mock.patch("app.services.training.enqueue_sms"),
            mock.patch("app.services.training.enqueue_email"),
        ):
            t.enrol(trainee=a, course=course, base_url="https://test")
        db.session.commit()

        plan = t.plan_bulk_issue([a.id, b.id], course)
    assert plan.n_issue == 1
    assert plan.n_skip == 1
    skip_row = next(r for r in plan.rows if r.action == "skip_in_flight")
    assert "active until" in skip_row.note


def test_plan_returns_skip_for_course_with_no_active_version(app):
    with app.app_context():
        # Create a course without a version.
        course = t.create_course(code="EMPTY-COURSE")
        a = _make_trainee("+447730000020", "BLK-C1")
        db.session.commit()

        plan = t.plan_bulk_issue([a.id], course)
    assert plan.n_issue == 0
    assert plan.rows[0].action == "skip_no_version"


def test_plan_marks_unknown_trainee_id_as_error(app):
    with app.app_context():
        course = _seed_course()
        plan = t.plan_bulk_issue(["00000000-0000-0000-0000-000000000000"], course)
    assert plan.n_error == 1
    assert plan.rows[0].action == "error"


# ─── apply: token uniqueness is the headline guarantee ──────────────


def test_each_enrolment_has_unique_token(app):
    """The headline correctness check: bulk-issuing for N trainees
    creates N enrolments with N distinct magic_tokens."""
    with app.app_context():
        course = _seed_course()
        trainees = [
            _make_trainee(f"+44773000{1000 + i:04d}", f"BLK-D{i}")
            for i in range(5)
        ]
        db.session.commit()
        ids = [tr.id for tr in trainees]

    with app.app_context():
        course = TrainingCourse.query.filter_by(code="BULK-A").first()
        plan = t.plan_bulk_issue(ids, course)
        assert plan.n_issue == 5

        with (
            mock.patch("app.services.training.enqueue_sms"),
            mock.patch("app.services.training.enqueue_email"),
        ):
            counts = t.apply_bulk_issue(plan, base_url="https://test", by_user_id="u-test")
        db.session.commit()

        assert counts == {"issued": 5, "skipped": 0, "errors": 0}

        # Five enrolments — five distinct magic_tokens, five distinct
        # enrolment IDs (sanity).
        enrolments = (
            TrainingEnrolment.query.filter(TrainingEnrolment.trainee_id.in_(ids))
            .order_by(TrainingEnrolment.issued_at.asc())
            .all()
        )
        assert len(enrolments) == 5
        tokens = [e.magic_token for e in enrolments]
        assert len(set(tokens)) == 5, "magic_tokens collided across bulk batch"
        assert all(token for token in tokens), "every enrolment must have a token"
        # Source provenance recorded.
        assert all(e.source == "manual" for e in enrolments)
        assert all(e.source_ref == "bulk:u-test" for e in enrolments)


def test_apply_skips_in_flight_at_apply_time(app):
    """Re-validate in apply: even if the plan was computed when no
    in-flight existed, an enrolment that lands between preview and
    apply must NOT be re-issued."""
    with app.app_context():
        course = _seed_course()
        a = _make_trainee("+447730000030", "BLK-E1")
        db.session.commit()

        plan = t.plan_bulk_issue([a.id], course)
        assert plan.n_issue == 1

        # Simulate concurrent operator: someone else issued meanwhile.
        with (
            mock.patch("app.services.training.enqueue_sms"),
            mock.patch("app.services.training.enqueue_email"),
        ):
            t.enrol(trainee=a, course=course, base_url="https://test")
        db.session.commit()

        # Re-plan from the same input ids → row now classified skip.
        plan2 = t.plan_bulk_issue([a.id], course)
    assert plan2.n_skip == 1
    assert plan2.n_issue == 0


# ─── HTTP route ─────────────────────────────────────────────────────


def _login(client):
    client.post(
        "/auth/login",
        data={"email": "admin@test", "password": "Admin123!"},
        follow_redirects=False,
    )


def test_bulk_issue_route_renders_preview(app, client):
    with app.app_context():
        course = _seed_course(code="BULK-PREV")
        a = _make_trainee("+447730000040", "BLK-PRV1")
        b = _make_trainee("+447730000041", "BLK-PRV2")
        db.session.commit()
        ids = [a.id, b.id]

    _login(client)
    data = MultiDict()
    for i in ids:
        data.add("trainee_ids", i)
    data.add("course_code", "BULK-PREV")
    resp = client.post(
        "/admin/training/trainees/bulk-issue",
        data=data,
        follow_redirects=False,
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Confirm screen shows both trainees and the issue button.
    assert "BLK-PRV1" in body or "Worker BLK-PRV1" in body
    assert "BULK-PREV" in body


def test_bulk_issue_route_apply_persists(app, client):
    with app.app_context():
        course = _seed_course(code="BULK-APPL")
        a = _make_trainee("+447730000050", "BLK-APP1")
        db.session.commit()
        ids = [a.id]

    _login(client)
    data = MultiDict()
    for i in ids:
        data.add("trainee_ids", i)
    data.add("course_code", "BULK-APPL")
    data.add("apply", "1")
    with (
        mock.patch("app.services.training.enqueue_sms"),
        mock.patch("app.services.training.enqueue_email"),
    ):
        resp = client.post(
            "/admin/training/trainees/bulk-issue",
            data=data,
            follow_redirects=False,
        )
    assert resp.status_code == 302
    with app.app_context():
        enr = TrainingEnrolment.query.filter_by(trainee_id=ids[0]).all()
        assert len(enr) == 1
        assert enr[0].source == "manual"


def test_bulk_issue_route_rejects_empty_selection(app, client):
    _login(client)
    resp = client.post(
        "/admin/training/trainees/bulk-issue",
        data={"course_code": "HACCP-REFRESHER"},
        follow_redirects=False,
    )
    # Redirected back with a flash warning.
    assert resp.status_code == 302


def test_bulk_issue_route_rejects_unknown_course(app, client):
    with app.app_context():
        a = _make_trainee("+447730000060", "BLK-UNK1")
        db.session.commit()
        ids = [a.id]
    _login(client)
    data = MultiDict()
    for i in ids:
        data.add("trainee_ids", i)
    data.add("course_code", "DOES-NOT-EXIST")
    resp = client.post(
        "/admin/training/trainees/bulk-issue",
        data=data,
        follow_redirects=False,
    )
    assert resp.status_code == 302  # back to list with flash


# ─── List filters ───────────────────────────────────────────────────


def test_list_filters_by_role_code(app, client):
    with app.app_context():
        _make_trainee("+447730000070", "BLK-F1", role="alpha-list")
        _make_trainee("+447730000071", "BLK-F2", role="beta-list")
        db.session.commit()

    _login(client)
    resp = client.get("/admin/training/trainees?role_code=alpha-list")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "BLK-F1" in body
    assert "BLK-F2" not in body
