"""CSV export / import for trainees + courses.

Covers:
- export_trainees / export_courses produce stable headers + UTF-8 BOM
- templates contain header + one example row
- plan_trainees_import classifies rows (create / update / error)
- apply_trainees_plan creates/updates Trainee rows correctly
- in-CSV duplicate phone / employee_number → flagged
- phone collision against existing trainee → flagged
- channel=email/both without email → flagged
- courses import: shell + version 1 created; existing course + code
  match → fresh version
- HTTP routes return CSV / template / preview correctly
"""

from __future__ import annotations

from app.extensions import db
from app.models import Trainee, TrainingCourse
from app.services import training as t
from app.services import training_csv


def _bom_decode(blob: bytes) -> str:
    return blob.decode("utf-8-sig")


# ─── exports ─────────────────────────────────────────────────────────


def test_trainee_export_has_bom_and_header(app):
    with app.app_context():
        blob = training_csv.export_trainees()
    assert blob.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = _bom_decode(blob)
    first_line = text.splitlines()[0]
    for col in (
        "employee_number", "phone", "email", "full_name",
        "role_code", "line_code", "language",
        "notification_channel", "is_active",
    ):
        assert col in first_line


def test_trainees_template_has_one_example_row(app):
    with app.app_context():
        blob = training_csv.trainees_template()
    text = _bom_decode(blob)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert "EMP-001" in lines[1]


def test_course_export_has_bom_and_header(app):
    with app.app_context():
        blob = training_csv.export_courses()
    assert blob.startswith(b"\xef\xbb\xbf")
    first = _bom_decode(blob).splitlines()[0]
    for col in ("code", "title_pl", "title_en", "validity_months"):
        assert col in first


# ─── trainees import — classification ────────────────────────────────


def _csv(*rows: tuple[str, ...]) -> bytes:
    """Build a CSV from python tuples."""
    import csv as _csv_mod
    import io as _io
    buf = _io.StringIO()
    buf.write("﻿")  # BOM
    w = _csv_mod.writer(buf, lineterminator="\n")
    w.writerow(training_csv.TRAINEE_COLUMNS)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def test_trainee_plan_creates_and_updates(app):
    with app.app_context():
        # Existing trainee — will be updated.
        existing = t.create_trainee(
            employee_number="EMP-OLD",
            phone="+447700100001",
            email="old@example.com",
            full_name="Old Name",
            role_code="operator",
        )
        db.session.commit()
        eid = existing.id

        blob = _csv(
            ("EMP-OLD", "+447700100001", "new@example.com", "New Name",
             "qa", "", "en", "sms", "true"),
            ("EMP-NEW", "+447700100002", "fresh@example.com", "Fresh",
             "operator", "", "en", "sms", "true"),
        )
        plan = training_csv.plan_trainees_import(blob)

        assert plan.n_create == 1
        assert plan.n_update == 1
        assert plan.n_error == 0

        # Update row carries the diff.
        upd = next(r for r in plan.rows if r.action == "update")
        assert upd.matched_trainee_id == eid
        assert "full_name" in upd.diff
        assert upd.diff["full_name"] == ("Old Name", "New Name")
        assert upd.diff["role_code"] == ("operator", "qa")


def test_trainee_plan_flags_in_csv_duplicate_emp_no(app):
    with app.app_context():
        blob = _csv(
            ("EMP-X", "+447700100010", "a@x.com", "A", "operator", "", "en", "sms", "true"),
            ("EMP-X", "+447700100011", "b@x.com", "B", "operator", "", "en", "sms", "true"),
        )
        plan = training_csv.plan_trainees_import(blob)
    err = next(r for r in plan.rows if r.errors)
    assert any("duplicate employee_number" in e for e in err.errors)


def test_trainee_plan_flags_phone_collision_with_other_trainee(app):
    with app.app_context():
        # Existing trainee owns +447700100020 and has emp EMP-A.
        t.create_trainee(
            employee_number="EMP-A", phone="+447700100020",
            email="a@example.com", full_name="A", role_code="operator",
        )
        db.session.commit()

        # CSV tries to introduce EMP-B with the same phone — must reject.
        blob = _csv(
            ("EMP-B", "+447700100020", "b@example.com", "B",
             "operator", "", "en", "sms", "true"),
        )
        plan = training_csv.plan_trainees_import(blob)
    err = plan.rows[0]
    assert err.errors
    assert any("phone" in e for e in err.errors)


def test_trainee_plan_flags_email_required_for_email_channel(app):
    with app.app_context():
        blob = _csv(
            ("EMP-E", "+447700100030", "", "E",
             "operator", "", "en", "email", "true"),
        )
        plan = training_csv.plan_trainees_import(blob)
    err = plan.rows[0]
    assert any("email is required" in e for e in err.errors)


def test_trainee_apply_persists_rows(app):
    with app.app_context():
        blob = _csv(
            ("EMP-AP1", "+447700100040", "x@y.com", "X",
             "operator", "", "en", "sms", "true"),
        )
        plan = training_csv.plan_trainees_import(blob)
        assert plan.n_error == 0
        counts = training_csv.apply_trainees_plan(plan)
        db.session.commit()
        assert counts["created"] == 1

        tr = Trainee.query.filter_by(employee_number="EMP-AP1").first()
        assert tr is not None
        assert tr.phone == "+447700100040"


# ─── courses import ──────────────────────────────────────────────────


def _courses_csv(*rows: tuple[str, ...]) -> bytes:
    import csv as _csv_mod
    import io as _io
    buf = _io.StringIO()
    buf.write("﻿")
    w = _csv_mod.writer(buf, lineterminator="\n")
    w.writerow(training_csv.COURSE_COLUMNS)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def test_course_plan_creates_and_updates(app):
    with app.app_context():
        # Existing course.
        old = t.create_course(code="OLD-COURSE")
        t.add_course_version(
            course=old,
            title={"pl": "Stary", "en": "Old"},
            summary={"pl": "", "en": ""},
        )
        db.session.commit()

        blob = _courses_csv(
            # Update of OLD-COURSE — fresh version on apply.
            ("OLD-COURSE", "true", "desc",
             "Stary v2", "Old v2", "", "", "0.7", "12", "7"),
            # New code.
            ("NEW-COURSE", "true", "Brand new",
             "Nowy", "New", "", "", "0.7", "12", "7"),
        )
        plan = training_csv.plan_courses_import(blob)
        assert plan.n_create == 1
        assert plan.n_update == 1
        assert plan.n_error == 0

        counts = training_csv.apply_courses_plan(plan)
        db.session.commit()
        assert counts["created"] == 1
        assert counts["updated"] == 1

        old_after = TrainingCourse.query.filter_by(code="OLD-COURSE").first()
        assert old_after is not None
        # Update creates a fresh version — should now be v2.
        assert len(old_after.versions) == 2

        new_course = TrainingCourse.query.filter_by(code="NEW-COURSE").first()
        assert new_course is not None
        assert new_course.active_version is not None


def test_course_plan_rejects_missing_titles(app):
    with app.app_context():
        blob = _courses_csv(
            ("CODE-X", "true", "", "", "", "", "", "0.7", "12", "7"),
        )
        plan = training_csv.plan_courses_import(blob)
    err = plan.rows[0]
    assert err.errors
    assert any("title_pl" in e for e in err.errors)


# ─── HTTP routes ─────────────────────────────────────────────────────


def _login(client, app):
    with app.app_context():
        from app.models import User
        admin = User.query.filter_by(email="admin@test").first()
        assert admin is not None
    client.post(
        "/auth/login",
        data={"email": "admin@test", "password": "Admin123!"},
        follow_redirects=False,
    )


def test_trainees_csv_route_returns_csv(app, client):
    _login(client, app)
    resp = client.get("/admin/training/trainees.csv")
    assert resp.status_code == 200
    assert resp.mimetype.startswith("text/csv")
    body = resp.get_data(as_text=True)
    assert "employee_number" in body


def test_trainees_template_route_returns_template(app, client):
    _login(client, app)
    resp = client.get("/admin/training/trainees/template.csv")
    assert resp.status_code == 200
    assert "EMP-001" in resp.get_data(as_text=True)


def test_courses_csv_route_returns_csv(app, client):
    _login(client, app)
    resp = client.get("/admin/training/courses.csv")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "title_pl" in body


def test_trainees_import_preview_renders(app, client):
    _login(client, app)
    blob = _csv(
        ("EMP-WEB", "+447700100090", "w@example.com", "Web", "operator", "", "en", "sms", "true"),
    )
    resp = client.post(
        "/admin/training/trainees/import",
        data={"csv": (open("/dev/null", "rb"), "")},  # no file → flash error
        content_type="multipart/form-data",
    )
    # No file → 200 with flash, no plan rendered.
    assert resp.status_code == 200

    # Now upload a real file.
    import io as _io
    resp2 = client.post(
        "/admin/training/trainees/import",
        data={"csv": (_io.BytesIO(blob), "trainees.csv")},
        content_type="multipart/form-data",
    )
    assert resp2.status_code == 200
    body = resp2.get_data(as_text=True)
    assert "EMP-WEB" in body
    # Preview shows "create" badge for the new row.
    assert "+ create" in body


def test_trainees_import_apply_persists(app, client):
    _login(client, app)
    blob = _csv(
        ("EMP-APP", "+447700100095", "ap@example.com", "Apply Person",
         "operator", "", "en", "sms", "true"),
    )
    import io as _io
    resp = client.post(
        "/admin/training/trainees/import",
        data={"csv": (_io.BytesIO(blob), "t.csv"), "apply": "1"},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302  # redirect after apply
    with app.app_context():
        tr = Trainee.query.filter_by(employee_number="EMP-APP").first()
        assert tr is not None
        assert tr.full_name == "Apply Person"


def test_trainees_import_rejects_oversize_upload(app, client):
    """Upload over MAX_CSV_UPLOAD_BYTES is rejected with the too_large flash
    and never reaches plan_trainees_import — bounds memory regardless of
    what the client tried to send.
    """
    from app.constants import MAX_CSV_UPLOAD_BYTES

    _login(client, app)
    import io as _io

    # One byte past the cap. Body is junk — the size check fires before
    # CSV parsing, so contents don't matter.
    oversize = b"x" * (MAX_CSV_UPLOAD_BYTES + 1)
    resp = client.post(
        "/admin/training/trainees/import",
        data={"csv": (_io.BytesIO(oversize), "huge.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Flash key surfaces in the rendered page; either the EN or PL
    # message variant, depending on the test user's locale.
    assert "too large" in body.lower() or "za duży" in body.lower()
    # And no preview was rendered (no "+ create" badge).
    assert "+ create" not in body


def test_courses_import_rejects_oversize_upload(app, client):
    from app.constants import MAX_CSV_UPLOAD_BYTES

    _login(client, app)
    import io as _io

    oversize = b"x" * (MAX_CSV_UPLOAD_BYTES + 1)
    resp = client.post(
        "/admin/training/courses/import",
        data={"csv": (_io.BytesIO(oversize), "huge.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "too large" in body.lower() or "za duży" in body.lower()
