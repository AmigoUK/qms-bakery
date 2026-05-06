"""Compliance state resolver + bulk matrix loader.

Resolver tests cover all 6 ComplianceState values. Bulk-loader test
asserts the query count stays O(1) regardless of grid size — that's
the N+1 fix that makes the dashboard usable at production scale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import event

from app.extensions import db
from app.models import (
    Trainee,
    TrainingAnswerOption,
    TrainingAssignment,
    TrainingCertification,
    TrainingCourse,
    TrainingModule,
    TrainingQuestion,
)
from app.services import training as t
from app.services.training import ComplianceState, compliance_state

# ─── helpers ────────────────────────────────────────────────────────


def _seed_one_course(code: str = "HACCP-MAT") -> TrainingCourse:
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
        course_version_id=version.id,
        order_index=0,
        prompt={"pl": "?", "en": "?"},
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
    return course


def _make_trainee(phone: str, role: str = "operator") -> Trainee:
    return t.create_trainee(phone=phone, full_name=phone, role_code=role)


def _make_cert(trainee: Trainee, course: TrainingCourse, *, valid_until: datetime) -> TrainingCertification:
    """Drive a full pass through the training service so attempt_id /
    declaration_id constraints are satisfied, then patch valid_until
    to whatever the test scenario needs."""
    enrolment = t.enrol(trainee=trainee, course=course, base_url="https://test")
    db.session.flush()
    attempt = t.start_attempt(enrolment)
    # Build the answer-set: every correct option for every question.
    questions = course.active_version.questions
    answers = {q.id: [o.id for o in q.options if o.is_correct] for q in questions}
    t.submit_attempt(enrolment, answers)
    t.record_declaration(
        attempt=attempt,
        typed_name=trainee.full_name,
        declaration_text="ok",
    )
    db.session.flush()
    cert = TrainingCertification.query.filter_by(
        trainee_id=trainee.id, course_id=course.id
    ).order_by(TrainingCertification.valid_until.desc()).first()
    assert cert is not None, "expected a cert after a passing attempt"
    cert.valid_until = valid_until
    db.session.flush()
    return cert


# ─── resolver — all 6 states ─────────────────────────────────────────


def test_state_not_required_when_no_assignment_no_cert(app):
    with app.app_context():
        course = _seed_one_course()
        trainee = _make_trainee("+447700000001")
        db.session.commit()

        state = compliance_state(
            trainee=trainee, course=course,
            is_required=False, cert=None, has_in_flight=False,
        )
    assert state is ComplianceState.NOT_REQUIRED


def test_state_extra_when_not_required_but_cert_valid(app):
    with app.app_context():
        course = _seed_one_course()
        trainee = _make_trainee("+447700000002")
        cert = _make_cert(trainee, course, valid_until=datetime.now(UTC) + timedelta(days=200))
        db.session.commit()

        state = compliance_state(
            trainee=trainee, course=course,
            is_required=False, cert=cert, has_in_flight=False,
        )
    assert state is ComplianceState.EXTRA


def test_state_valid_when_required_and_outside_lead_window(app):
    with app.app_context():
        course = _seed_one_course()
        trainee = _make_trainee("+447700000003")
        # 60 days out, lead is 14 days → comfortably VALID.
        cert = _make_cert(trainee, course, valid_until=datetime.now(UTC) + timedelta(days=60))
        db.session.commit()

        state = compliance_state(
            trainee=trainee, course=course,
            is_required=True, cert=cert, has_in_flight=False,
        )
    assert state is ComplianceState.VALID


def test_state_due_soon_when_inside_lead_window(app):
    with app.app_context():
        course = _seed_one_course()
        trainee = _make_trainee("+447700000004")
        # 5 days out — within the 14-day lead window.
        cert = _make_cert(trainee, course, valid_until=datetime.now(UTC) + timedelta(days=5))
        db.session.commit()

        state = compliance_state(
            trainee=trainee, course=course,
            is_required=True, cert=cert, has_in_flight=False,
        )
    assert state is ComplianceState.DUE_SOON


def test_state_overdue_when_required_and_no_cert(app):
    with app.app_context():
        course = _seed_one_course()
        trainee = _make_trainee("+447700000005")
        db.session.commit()

        state = compliance_state(
            trainee=trainee, course=course,
            is_required=True, cert=None, has_in_flight=False,
        )
    assert state is ComplianceState.OVERDUE


def test_state_overdue_when_cert_expired(app):
    with app.app_context():
        course = _seed_one_course()
        trainee = _make_trainee("+447700000006")
        cert = _make_cert(trainee, course, valid_until=datetime.now(UTC) - timedelta(days=1))
        db.session.commit()

        state = compliance_state(
            trainee=trainee, course=course,
            is_required=True, cert=cert, has_in_flight=False,
        )
    assert state is ComplianceState.OVERDUE


def test_state_in_flight_takes_precedence_over_overdue(app):
    """An open enrolment beats expired cert — recert is in progress."""
    with app.app_context():
        course = _seed_one_course()
        trainee = _make_trainee("+447700000007")
        cert = _make_cert(trainee, course, valid_until=datetime.now(UTC) - timedelta(days=10))
        db.session.commit()

        state = compliance_state(
            trainee=trainee, course=course,
            is_required=True, cert=cert, has_in_flight=True,
        )
    assert state is ComplianceState.IN_FLIGHT


def test_state_in_flight_shown_even_when_not_required(app):
    """Plant manager wants to see active enrolments regardless."""
    with app.app_context():
        course = _seed_one_course()
        trainee = _make_trainee("+447700000008")
        db.session.commit()

        state = compliance_state(
            trainee=trainee, course=course,
            is_required=False, cert=None, has_in_flight=True,
        )
    assert state is ComplianceState.IN_FLIGHT


# ─── _assignment_matches helper ──────────────────────────────────────


def test_assignment_wildcards_match_any_role_or_line(app):
    from app.services.training import _assignment_matches

    with app.app_context():
        course = _seed_one_course()
        trainee = _make_trainee("+447700000009", role="operator")
        # Both NULL → matches any.
        a_any = TrainingAssignment(course_id=course.id, role_code=None, line_id=None)
        # Role-specific match.
        a_op = TrainingAssignment(course_id=course.id, role_code="operator", line_id=None)
        # Role-mismatch.
        a_qa = TrainingAssignment(course_id=course.id, role_code="qa", line_id=None)
        db.session.add_all([a_any, a_op, a_qa])
        db.session.flush()

        assert _assignment_matches(a_any, trainee) is True
        assert _assignment_matches(a_op, trainee) is True
        assert _assignment_matches(a_qa, trainee) is False


# ─── bulk-loader query count ────────────────────────────────────────


def test_bulk_loader_query_count_is_constant(app):
    """The matrix loader must stay O(1) in queries regardless of grid
    size — that's the whole point of bulk-loading. We measure with
    SQLAlchemy's `before_cursor_execute` event listener and assert
    the count is at most 8 for a 5-trainee × 3-course grid (and the
    same count for any larger grid).

    Trainees use a synthetic role so they don't get auto-required
    by the seeded demo course's operator-role Assignment, which
    would otherwise inflate the grid.
    """
    from app.services.training import build_compliance_matrix

    with app.app_context():
        # Build 3 courses, 5 trainees, with a sprinkle of certs +
        # assignments so every code path in the loader fires.
        courses = []
        for i, code in enumerate(("HACCP-A", "HACCP-B", "HACCP-C")):
            c = _seed_one_course(code=code)
            courses.append(c)
            # Course A is required for the matrix-test role.
            if i == 0:
                db.session.add(
                    TrainingAssignment(course_id=c.id, role_code="matrix-test", line_id=None)
                )
        trainees = [_make_trainee(f"+44770010000{i}", role="matrix-test") for i in range(5)]
        # Give first trainee a valid cert in course A; second an expired cert in B.
        _make_cert(trainees[0], courses[0], valid_until=datetime.now(UTC) + timedelta(days=60))
        _make_cert(trainees[1], courses[1], valid_until=datetime.now(UTC) - timedelta(days=10))
        db.session.commit()

        engine = db.session.get_bind()
        query_count = {"n": 0}

        @event.listens_for(engine, "before_cursor_execute")
        def _count(conn, cursor, statement, parameters, context, executemany):
            query_count["n"] += 1

        try:
            matrix = build_compliance_matrix()
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        # Assert grid integrity. The demo course (HACCP-REFRESHER)
        # is also seeded by conftest, so the matrix has 4 courses.
        assert len(matrix["trainees"]) == 5
        assert len(matrix["courses"]) >= 3
        # The 5 × N grid must include one cell per (trainee, course).
        assert len(matrix["states"]) == 5 * len(matrix["courses"])

        # Assert constant query count. Tighten the bound if regressions
        # creep in. Loose upper bound chosen to leave some headroom for
        # SQLAlchemy bookkeeping; the meaningful guarantee is "doesn't
        # scale with cells" — see the bigger-grid sanity check below.
        assert query_count["n"] <= 8, f"expected ≤ 8 queries, got {query_count['n']}"


def test_bulk_loader_does_not_scale_with_grid_size(app):
    """Double the grid; the query count must not double. This is the
    canary that catches accidental N+1 reintroductions."""
    from app.services.training import build_compliance_matrix

    with app.app_context():
        # 5 courses × 10 trainees = 50 cells. Use synthetic role so
        # the demo seeded course doesn't auto-required-flag them.
        courses = [_seed_one_course(code=f"COURSE-{i}") for i in range(5)]
        for c in courses:
            db.session.add(TrainingAssignment(course_id=c.id, role_code="matrix-test", line_id=None))
        for i in range(10):
            _make_trainee(f"+44770020000{i:02d}", role="matrix-test")
        db.session.commit()

        engine = db.session.get_bind()
        query_count = {"n": 0}

        @event.listens_for(engine, "before_cursor_execute")
        def _count(conn, cursor, statement, parameters, context, executemany):
            query_count["n"] += 1

        try:
            matrix = build_compliance_matrix()
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        # The demo HACCP-REFRESHER is also seeded → 6 courses × 10 trainees.
        assert len(matrix["states"]) == 10 * len(matrix["courses"])
        assert query_count["n"] <= 8, f"N+1 leak suspected: {query_count['n']} queries"


def test_bulk_loader_kpis_reflect_grid_state(app):
    from app.services.training import build_compliance_matrix

    with app.app_context():
        course_a = _seed_one_course(code="REQ-A")
        course_b = _seed_one_course(code="REQ-B")
        # Both courses required for our synthetic test role only.
        db.session.add_all([
            TrainingAssignment(course_id=course_a.id, role_code="kpi-test", line_id=None),
            TrainingAssignment(course_id=course_b.id, role_code="kpi-test", line_id=None),
        ])
        # Three trainees.
        t1 = _make_trainee("+447700300001", role="kpi-test")  # Both valid → cleared.
        t2 = _make_trainee("+447700300002", role="kpi-test")  # One overdue → blocked.
        t3 = _make_trainee("+447700300003", role="kpi-test")  # Both overdue → blocked, two overdue cells.

        _make_cert(t1, course_a, valid_until=datetime.now(UTC) + timedelta(days=60))
        _make_cert(t1, course_b, valid_until=datetime.now(UTC) + timedelta(days=60))
        _make_cert(t2, course_a, valid_until=datetime.now(UTC) + timedelta(days=60))
        # t2 has no cert for course_b → OVERDUE.
        # t3 has nothing → OVERDUE in both cells.
        db.session.commit()

        matrix = build_compliance_matrix()

    assert matrix["cleared"][t1.id] is True
    assert matrix["cleared"][t2.id] is False
    assert matrix["cleared"][t3.id] is False
    # KPIs:
    assert matrix["kpis"]["blocked"] == 2  # t2, t3
    # Both courses have at least one OVERDUE cell.
    assert matrix["kpis"]["overdue_courses"] == 2


# ─── Dashboard HTML rendering ────────────────────────────────────────


def test_dashboard_renders_color_states(app, client, login_admin):
    """Live rendering check: every state class lives in the HTML when
    the underlying scenario covers it."""
    with app.app_context():
        course_a = _seed_one_course(code="HTML-A")
        course_b = _seed_one_course(code="HTML-B")
        course_c = _seed_one_course(code="HTML-C")
        # course_a + course_b required for the synthetic role.
        db.session.add_all([
            TrainingAssignment(course_id=course_a.id, role_code="render-test", line_id=None),
            TrainingAssignment(course_id=course_b.id, role_code="render-test", line_id=None),
        ])
        # Two trainees: one fully covered, one with a mix of states.
        t1 = _make_trainee("+447700400001", role="render-test")
        t2 = _make_trainee("+447700400002", role="render-test")
        # t1 → VALID on A (60 days out).
        _make_cert(t1, course_a, valid_until=datetime.now(UTC) + timedelta(days=60))
        # t1 → DUE_SOON on B (5 days out, within 14-day lead).
        _make_cert(t1, course_b, valid_until=datetime.now(UTC) + timedelta(days=5))
        # t1 → EXTRA on C (not required, valid cert).
        _make_cert(t1, course_c, valid_until=datetime.now(UTC) + timedelta(days=60))
        # t2 → OVERDUE on A (no cert).
        # t2 → IN_FLIGHT on B (issue a magic-link, status STARTED).
        enr = t.enrol(trainee=t2, course=course_b, base_url="https://test")
        enr.status = "started"
        # t2 → NOT_REQUIRED on C (no cert, no assignment).
        db.session.commit()

    login_admin()
    resp = client.get("/admin/training/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Every colour class lives in the page now.
    for klass in (
        "compliance-valid",
        "compliance-due-soon",
        "compliance-in-flight",
        "compliance-overdue",
        "compliance-extra",
        "compliance-not-required",
    ):
        assert klass in body, f"missing {klass!r} in rendered dashboard"

    # Worker-clearance badges land too.
    assert "clearance-badge" in body
    assert "row-blocked" in body  # t2 is blocked by OVERDUE cell


# ─── Filters ─────────────────────────────────────────────────────────


def test_matrix_filters_by_role(app):
    """role_code filter narrows the trainee set at the SQL layer."""
    from app.services.training import build_compliance_matrix

    with app.app_context():
        course = _seed_one_course(code="FLT-A")
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code=None, line_id=None)
        )
        _make_trainee("+447710000001", role="alpha-role")
        _make_trainee("+447710000002", role="alpha-role")
        _make_trainee("+447710000003", role="beta-role")
        db.session.commit()

        all_matrix = build_compliance_matrix()
        alpha_matrix = build_compliance_matrix(role_code="alpha-role")

    # Filter must drop the beta trainee.
    assert len(alpha_matrix["trainees"]) == 2
    assert all(t.role_code == "alpha-role" for t in alpha_matrix["trainees"])
    # Without the filter, all three are present.
    role_codes = {t.role_code for t in all_matrix["trainees"]}
    assert "alpha-role" in role_codes and "beta-role" in role_codes


def test_matrix_filters_by_blocked_only(app):
    """blocked=1 drops fully-cleared workers."""
    from app.services.training import build_compliance_matrix

    with app.app_context():
        course = _seed_one_course(code="FLT-B")
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code="block-test", line_id=None)
        )
        # cleared trainee — has a fresh cert.
        cleared_t = _make_trainee("+447710000010", role="block-test")
        _make_cert(cleared_t, course, valid_until=datetime.now(UTC) + timedelta(days=60))
        # blocked trainee — no cert at all.
        blocked_t = _make_trainee("+447710000011", role="block-test")
        db.session.commit()

        only_blocked = build_compliance_matrix(role_code="block-test", blocked_only=True)

    visible_ids = {t.id for t in only_blocked["trainees"]}
    assert cleared_t.id not in visible_ids
    assert blocked_t.id in visible_ids
    # KPI reflects the filtered subset.
    assert only_blocked["kpis"]["blocked"] == 1


def test_dashboard_route_honours_filters(app, client, login_admin):
    with app.app_context():
        course = _seed_one_course(code="FLT-ROUTE")
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code="route-test", line_id=None)
        )
        _make_trainee("+447710000020", role="route-test")
        _make_trainee("+447710000021", role="other-role")
        db.session.commit()

    login_admin()
    resp = client.get("/admin/training/dashboard?role_code=route-test")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The filter-form should pre-select route-test (no need to assert on
    # the data payload; presence of the value=... selected is enough).
    assert 'value="route-test"' in body


# ─── Drill-down route ────────────────────────────────────────────────


def test_trainee_course_history_route_renders(app, client, login_admin):
    with app.app_context():
        course = _seed_one_course(code="DRILL")
        trainee = _make_trainee("+447710000050", role="drill")
        cert = _make_cert(trainee, course, valid_until=datetime.now(UTC) + timedelta(days=30))
        db.session.commit()
        trainee_id = trainee.id
        course_id = course.id
        cert_until = cert.valid_until.strftime("%Y-%m-%d")

    login_admin()
    resp = client.get(
        f"/admin/training/trainees/{trainee_id}/courses/{course_id}/history"
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Sections render headings + the cert appears with its valid_until.
    assert "Certifications" in body
    assert "Enrolments" in body
    assert "Attempts" in body
    assert cert_until in body


def test_trainee_course_history_route_404_for_unknown(app, client, login_admin):
    login_admin()
    resp = client.get("/admin/training/trainees/missing/courses/missing/history")
    assert resp.status_code == 404


def test_matrix_filters_multi_role(app):
    """Two role_codes in the list both narrow the visible set."""
    from app.services.training import build_compliance_matrix

    with app.app_context():
        course = _seed_one_course(code="MUL-A")
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code=None, line_id=None)
        )
        _make_trainee("+447720000001", role="alpha")
        _make_trainee("+447720000002", role="beta")
        _make_trainee("+447720000003", role="gamma")
        db.session.commit()

        m = build_compliance_matrix(role_codes=["alpha", "beta"])

    visible_roles = {t.role_code for t in m["trainees"]}
    assert visible_roles == {"alpha", "beta"}


def test_dashboard_route_accepts_multi_role(app, client, login_admin):
    with app.app_context():
        course = _seed_one_course(code="MUL-ROUTE")
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code=None, line_id=None)
        )
        _make_trainee("+447720000010", role="alpha-route")
        _make_trainee("+447720000011", role="beta-route")
        db.session.commit()

    login_admin()
    resp = client.get("/admin/training/dashboard?role_code=alpha-route&role_code=beta-route")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Both options selected in the multi-select rendered.
    assert 'value="alpha-route"' in body
    assert 'value="beta-route"' in body


def test_drill_down_issue_link_button_posts_and_enrols(app, client, login_admin):
    """POST to the issue endpoint creates an enrolment for that pair."""
    from unittest import mock

    from app.models import TrainingEnrolment

    with app.app_context():
        course = _seed_one_course(code="ISSUE-NOW")
        trainee = _make_trainee("+447720000020", role="issue-test")
        db.session.commit()
        trainee_id = trainee.id
        course_id = course.id

    login_admin()
    # Suppress the channel-delivery side-effects so the test doesn't
    # need a real Redis or SMTP.
    with (
        mock.patch("app.services.training.enqueue_sms"),
        mock.patch("app.services.training.enqueue_email"),
    ):
        resp = client.post(
            f"/admin/training/trainees/{trainee_id}/courses/{course_id}/issue",
            follow_redirects=False,
        )
    assert resp.status_code == 302
    # Redirect back to the history page for the same pair.
    assert "/history" in resp.headers["Location"]

    with app.app_context():
        enr = (
            TrainingEnrolment.query.filter_by(trainee_id=trainee_id)
            .order_by(TrainingEnrolment.issued_at.desc())
            .first()
        )
        assert enr is not None
        assert enr.course_version.course_id == course_id


def test_pdf_orientation_auto_landscape_when_many_courses(app, client, login_admin):
    """6+ course columns → PDF auto-flips landscape via the ?orient
    fallback. Detect by inspecting the rendered HTML before WeasyPrint
    consumes it (we patch the renderer to capture the html arg)."""
    from unittest import mock

    with app.app_context():
        # Six courses, all required for the same role.
        for i in range(6):
            c = _seed_one_course(code=f"LAND-{i}")
            db.session.add(
                TrainingAssignment(course_id=c.id, role_code="land-test", line_id=None)
            )
        _make_trainee("+447720000030", role="land-test")
        db.session.commit()

    login_admin()
    captured = {}
    real_html = __import__("weasyprint").HTML

    def _capture(*args, **kwargs):
        captured["string"] = kwargs.get("string", args[0] if args else "")
        return real_html(*args, **kwargs)

    with mock.patch("weasyprint.HTML", side_effect=_capture):
        resp = client.get("/admin/training/dashboard.pdf?role_code=land-test")
    assert resp.status_code == 200
    # Auto-landscape because >5 courses.
    assert "@page { size: A4 landscape; }" in captured["string"]


def test_pdf_orientation_override_portrait_wins(app, client, login_admin):
    """?orient=portrait beats the auto-landscape default even with
    many courses."""
    from unittest import mock

    with app.app_context():
        for i in range(7):
            c = _seed_one_course(code=f"OVR-{i}")
            db.session.add(
                TrainingAssignment(course_id=c.id, role_code="override-test", line_id=None)
            )
        _make_trainee("+447720000040", role="override-test")
        db.session.commit()

    login_admin()
    captured = {}
    real_html = __import__("weasyprint").HTML

    def _capture(*args, **kwargs):
        captured["string"] = kwargs.get("string", args[0] if args else "")
        return real_html(*args, **kwargs)

    with mock.patch("weasyprint.HTML", side_effect=_capture):
        resp = client.get(
            "/admin/training/dashboard.pdf?role_code=override-test&orient=portrait"
        )
    assert resp.status_code == 200
    assert "landscape" not in captured["string"]


def test_dashboard_csv_export(app, client, login_admin):
    """CSV export carries the full grid + UTF-8 BOM for Excel."""
    with app.app_context():
        course = _seed_one_course(code="CSV-A")
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code="csv-test", line_id=None)
        )
        _make_trainee("+447710000060", role="csv-test")
        db.session.commit()

    login_admin()
    resp = client.get("/admin/training/dashboard.csv?role_code=csv-test")
    assert resp.status_code == 200
    assert resp.mimetype.startswith("text/csv")
    body = resp.get_data(as_text=True)
    # BOM for Excel.
    assert body.startswith("﻿")
    # Header + at least the trainee row.
    assert "Worker" in body
    assert "CSV-A" in body
    assert "+447710000060" in body or "csv-test" in body  # role is in column 2


def test_dashboard_pdf_export(app, client, login_admin):
    """PDF export renders without raising; content-type is application/pdf."""
    with app.app_context():
        course = _seed_one_course(code="PDF-A")
        db.session.add(
            TrainingAssignment(course_id=course.id, role_code="pdf-test", line_id=None)
        )
        _make_trainee("+447710000061", role="pdf-test")
        db.session.commit()

    login_admin()
    resp = client.get("/admin/training/dashboard.pdf?role_code=pdf-test")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    # PDF magic bytes.
    assert resp.data[:4] == b"%PDF"


def test_dashboard_cells_link_to_drill_down(app, client, login_admin):
    """Each compliance cell renders inside an <a> pointing at the
    history route. Plant manager can click straight from the matrix."""
    with app.app_context():
        course = _seed_one_course(code="LINK")
        trainee = _make_trainee("+447710000051", role="drill-link")
        db.session.commit()
        trainee_id = trainee.id
        course_id = course.id

    login_admin()
    resp = client.get("/admin/training/dashboard")
    body = resp.get_data(as_text=True)
    expected_href = f"/admin/training/trainees/{trainee_id}/courses/{course_id}/history"
    assert expected_href in body
