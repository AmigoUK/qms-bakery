"""End-to-end magic-link flow test through the public Flask routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models import (
    EnrolmentStatus,
    TrainingAnswerOption,
    TrainingCertification,
    TrainingDeclaration,
    TrainingModule,
    TrainingQuestion,
)
from app.services import training as t


def _seed_full_course():
    course = t.create_course(code="HACCP-FLOW")
    version = t.add_course_version(
        course=course,
        title={"pl": "HACCP", "en": "HACCP"},
        summary={"pl": "", "en": ""},
        pass_threshold=0.5,
        link_ttl_days=7,
    )
    db.session.add(
        TrainingModule(
            course_version_id=version.id,
            order_index=0,
            title={"pl": "M", "en": "M"},
            body_md={"pl": "<p>Moduł</p>", "en": "<p>Module</p>"},
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


def _enrol(course):
    trainee = t.create_trainee(
        phone="+447700009999", full_name="Flow User", role_code="operator"
    )
    return t.enrol(trainee=trainee, course=course, base_url="https://qms.test")


def test_landing_renders_for_valid_token(app, client):
    with app.app_context():
        course, _, _ = _seed_full_course()
        enrolment = _enrol(course)
        db.session.commit()
        token = enrolment.magic_token
    resp = client.get(f"/training/take/{token}")
    assert resp.status_code == 200
    assert b"Start course" in resp.data or b"Resume" in resp.data


def test_invalid_token_returns_expired_page(client):
    resp = client.get("/training/take/garbage.token.value")
    assert resp.status_code == 410
    assert b"Link no longer valid" in resp.data or b"Link nie jest" in resp.data


def test_full_pass_flow_through_routes(app, client):
    with app.app_context():
        course, version, q = _seed_full_course()
        enrolment = _enrol(course)
        db.session.commit()
        token = enrolment.magic_token
        correct_id = next(o.id for o in q.options if o.is_correct)

    # 1. Land on the magic link
    resp = client.get(f"/training/take/{token}")
    assert resp.status_code == 200

    # 2. Start course (POST → redirect to module)
    resp = client.post(f"/training/take/{token}/start", follow_redirects=False)
    assert resp.status_code == 302
    assert "/module" in resp.headers["Location"]

    # 3. Module page
    resp = client.get(f"/training/take/{token}/module")
    assert resp.status_code == 200

    # 4. Advance past last module → redirected to exam
    resp = client.post(f"/training/take/{token}/next", follow_redirects=False)
    assert resp.status_code == 302
    assert "/exam" in resp.headers["Location"]

    # 5. Exam GET
    resp = client.get(f"/training/take/{token}/exam")
    assert resp.status_code == 200
    assert b"Submit answers" in resp.data

    # 6. Submit correct answer → redirect to declaration
    resp = client.post(
        f"/training/take/{token}/exam",
        data={f"q-{q.id}": correct_id},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/declaration" in resp.headers["Location"]

    # 7. Declaration GET
    resp = client.get(f"/training/take/{token}/declaration")
    assert resp.status_code == 200
    assert b"Type your full name" in resp.data

    # 8. POST declaration → redirect to result
    resp = client.post(
        f"/training/take/{token}/declare",
        data={
            "typed_name": "Flow User",
            "declaration_text": "I confirm ...",
            "signature_data_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkAAAAAAQA",
            "agree": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/result" in resp.headers["Location"]

    # 9. Result page shows pass + cert link
    resp = client.get(f"/training/take/{token}/result")
    assert resp.status_code == 200
    assert b"Passed" in resp.data
    assert b"Download certificate" in resp.data

    # 10. Cert PDF
    resp = client.get(f"/training/take/{token}/certificate")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data[:4] == b"%PDF"

    # 11. Re-opening the link goes to result (no second exam)
    resp = client.get(f"/training/take/{token}", follow_redirects=False)
    assert resp.status_code == 302
    assert "/result" in resp.headers["Location"]

    # 12. Database state: cert + declaration persisted
    with app.app_context():
        cert = TrainingCertification.query.first()
        decl = TrainingDeclaration.query.first()
        assert cert is not None
        assert decl is not None
        assert decl.typed_name == "Flow User"
        assert decl.signature_png is not None
        assert decl.ip_address is not None


def test_fail_flow_no_certificate(app, client):
    with app.app_context():
        course, _, q = _seed_full_course()
        enrolment = _enrol(course)
        db.session.commit()
        token = enrolment.magic_token
        wrong_id = next(o.id for o in q.options if not o.is_correct)

    client.post(f"/training/take/{token}/start")
    client.post(f"/training/take/{token}/next")
    resp = client.post(
        f"/training/take/{token}/exam",
        data={f"q-{q.id}": wrong_id},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/result" in resp.headers["Location"]

    resp = client.get(f"/training/take/{token}/result")
    assert resp.status_code == 200
    assert b"Not passed" in resp.data

    # No cert created, no declaration captured
    with app.app_context():
        assert TrainingCertification.query.first() is None
        assert TrainingDeclaration.query.first() is None


def test_certificate_404_for_unpassed_attempt(app, client):
    with app.app_context():
        course, _, q = _seed_full_course()
        enrolment = _enrol(course)
        db.session.commit()
        token = enrolment.magic_token
        wrong_id = next(o.id for o in q.options if not o.is_correct)

    client.post(f"/training/take/{token}/start")
    client.post(f"/training/take/{token}/next")
    client.post(
        f"/training/take/{token}/exam",
        data={f"q-{q.id}": wrong_id},
    )
    resp = client.get(f"/training/take/{token}/certificate")
    assert resp.status_code == 404


def test_expired_enrolment_yields_expired_page(app, client):
    with app.app_context():
        course, _, _ = _seed_full_course()
        enrolment = _enrol(course)
        # Force-expire
        enrolment.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.commit()
        token = enrolment.magic_token
    resp = client.get(f"/training/take/{token}")
    assert resp.status_code == 410
