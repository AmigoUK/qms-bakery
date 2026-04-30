"""Admin pages: course versioning on edit, trainee CRUD, dashboard,
permission gating.
"""

from __future__ import annotations

from app.extensions import db
from app.models import (
    EnrolmentStatus,
    TrainingCourse,
    TrainingCourseVersion,
    Trainee,
)
from app.permissions import Perm
from app.services import training as t


def _login(client, app, *, role: str = "compliance"):
    """Mint a User with the given role and log them in."""
    from app.auth import hash_password
    from app.models import Role, User

    with app.app_context():
        role_row = Role.query.filter_by(code=role).first()
        user = User.query.filter_by(email="admin@test").first()
        # The seed admin already has admin role. Re-use unless we need a different role.
        if role != "admin" and role_row is not None and user.role.code != role:
            user.role_id = role_row.id
            db.session.commit()
    return client.post(
        "/auth/login",
        data={"email": "admin@test", "password": "Admin123!"},
        follow_redirects=False,
    )


def test_admin_routes_require_permission(client):
    """An anonymous client gets redirected to login."""
    resp = client.get("/admin/training/trainees", follow_redirects=False)
    assert resp.status_code in (302, 401)
    if resp.status_code == 302:
        assert "/auth/login" in resp.headers["Location"]


def test_create_course_then_save_version_increments(app, client):
    _login(client, app)
    # Create the course shell
    resp = client.post(
        "/admin/training/courses/new",
        data={"code": "TEST-COURSE", "description": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 302  # redirects to courses_edit
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="TEST-COURSE").first()
        assert course is not None
        assert len(course.versions) == 0
        course_id = course.id

    # Save first version
    resp = client.post(
        f"/admin/training/courses/{course_id}",
        data={
            "title_pl": "Tytuł",
            "title_en": "Title",
            "summary_pl": "",
            "summary_en": "",
            "pass_threshold": "0.7",
            "validity_months": "12",
            "link_ttl_days": "7",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        course = db.session.get(TrainingCourse, course_id)
        assert len(course.versions) == 1
        assert course.active_version.version == 1

    # Edit → save again → new version, old becomes inactive
    client.post(
        f"/admin/training/courses/{course_id}",
        data={
            "title_pl": "Tytuł v2",
            "title_en": "Title v2",
            "summary_pl": "",
            "summary_en": "",
            "pass_threshold": "0.8",
            "validity_months": "12",
            "link_ttl_days": "7",
        },
    )
    with app.app_context():
        course = db.session.get(TrainingCourse, course_id)
        assert len(course.versions) == 2
        assert course.active_version.version == 2
        old_v1 = next(v for v in course.versions if v.version == 1)
        assert old_v1.is_active is False


def test_create_trainee_through_admin_form(app, client):
    _login(client, app)
    resp = client.post(
        "/admin/training/trainees/new",
        data={
            "phone": "+447700099999",
            "full_name": "Admin-Created Worker",
            "role_code": "operator",
            "line_code": "",
            "language": "en",
            "is_active": "y",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        trainee = Trainee.query.filter_by(phone="+447700099999").first()
        assert trainee is not None
        assert trainee.full_name == "Admin-Created Worker"


def test_dashboard_renders(app, client):
    _login(client, app)
    with app.app_context():
        course = t.create_course(code="HACCP-DASH")
        t.add_course_version(
            course=course,
            title={"pl": "T", "en": "T"},
            summary={"pl": "", "en": ""},
        )
        t.create_trainee(
            phone="+447700088888", full_name="Dashboard User", role_code="operator"
        )
        db.session.commit()
    resp = client.get("/admin/training/dashboard")
    assert resp.status_code == 200
    assert b"Dashboard User" in resp.data
    assert b"HACCP-DASH" in resp.data
    # Trainee with no cert and no open enrolment shows as Due
    assert b"Due" in resp.data


def test_issue_link_route_creates_enrolment(app, client):
    _login(client, app)
    with app.app_context():
        course = t.create_course(code="HACCP-ISSUE")
        t.add_course_version(
            course=course,
            title={"pl": "T", "en": "T"},
            summary={"pl": "", "en": ""},
        )
        trainee = t.create_trainee(
            phone="+447700077777", full_name="Issue User", role_code="operator"
        )
        db.session.commit()
        trainee_id = trainee.id

    resp = client.post(
        f"/admin/training/trainees/{trainee_id}/issue",
        data={"course_code": "HACCP-ISSUE"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        from app.models import TrainingEnrolment
        rows = TrainingEnrolment.query.filter_by(trainee_id=trainee_id).all()
        assert len(rows) == 1
        assert rows[0].status == EnrolmentStatus.ISSUED.value


def test_compliance_role_has_training_perms(app):
    with app.app_context():
        from app.models import Role
        compliance = Role.query.filter_by(code="compliance").first()
        codes = {p.code for p in compliance.permissions}
        assert Perm.TRAINING_AUTHOR in codes
        assert Perm.TRAINING_REVIEW in codes
        assert Perm.TRAINING_SEND in codes


def test_plant_manager_has_review_and_send_only(app):
    with app.app_context():
        from app.models import Role
        plant = Role.query.filter_by(code="plant_manager").first()
        codes = {p.code for p in plant.permissions}
        assert Perm.TRAINING_REVIEW in codes
        assert Perm.TRAINING_SEND in codes
        assert Perm.TRAINING_AUTHOR not in codes


def test_operator_has_no_training_admin_perms(app):
    with app.app_context():
        from app.models import Role
        op = Role.query.filter_by(code="operator").first()
        codes = {p.code for p in op.permissions}
        assert Perm.TRAINING_AUTHOR not in codes
        assert Perm.TRAINING_REVIEW not in codes
        assert Perm.TRAINING_SEND not in codes
