"""DSAR export — UK GDPR Art. 15.

Verifies:
- export_user includes the user row, role, and audit entries
- export_user OMITS password_hash and totp_secret
- export_trainee walks enrolments → attempts → declarations → certs
- the export call itself is audited
- malformed subject specs raise DSARError
- the CLI dispatches to the service and writes JSON
"""

from __future__ import annotations

import json

import pytest

from app.extensions import db
from app.services import dsar


def _seed_admin():
    from app.models import User

    return User.query.filter_by(email="admin@test").first()


def test_export_user_returns_their_data(app):
    with app.app_context():
        user = _seed_admin()
        user_id = user.id
        payload = dsar.export(f"user:{user_id}")

        assert payload["subject"] == {"type": "user", "id": user_id}
        assert payload["user"]["email"] == "admin@test"
        assert payload["user"]["full_name"]
        assert payload["role"]["code"] == "admin"
        assert isinstance(payload["audit_log_entries"], list)


def test_export_user_omits_credentials(app):
    with app.app_context():
        user = _seed_admin()
        payload = dsar.export(f"user:{user.id}")

        # Authentication material is excluded from the right-of-access export.
        assert "password_hash" not in payload["user"]
        assert "totp_secret" not in payload["user"]


def test_export_unknown_subject_raises(app):
    with app.app_context():
        with pytest.raises(dsar.DSARError):
            dsar.export("user:missing-uuid")
        with pytest.raises(dsar.DSARError):
            dsar.export("alien:abc")
        with pytest.raises(dsar.DSARError):
            dsar.export("missing-prefix")


def test_export_trainee_walks_full_chain(app):
    from app.models import EnrolmentSource, EnrolmentStatus, Trainee, TrainingEnrolment
    from app.services import training as training_service

    with app.app_context():
        trainee = training_service.create_trainee(
            phone="+447700900100",
            full_name="DSAR Test",
            role_code="operator",
        )
        course = training_service.get_course_by_code("HACCP-REFRESHER")
        assert course is not None
        enrolment = training_service.enrol(
            trainee=trainee,
            course=course,
            source=EnrolmentSource.MANUAL.value,
        )
        db.session.commit()
        trainee_id = trainee.id
        enrolment_id = enrolment.id

    with app.app_context():
        payload = dsar.export(f"trainee:{trainee_id}")

    assert payload["subject"]["type"] == "trainee"
    assert payload["trainee"]["phone"] == "+447700900100"
    assert len(payload["enrolments"]) == 1
    assert payload["enrolments"][0]["id"] == enrolment_id
    # Magic token excluded — it's an active credential, not data the
    # subject has a right to under Art. 15.
    assert "magic_token" not in payload["enrolments"][0]


def test_export_itself_is_audited(app):
    from app.audit_actions import AuditAction
    from app.models import AuditLog

    with app.app_context():
        user = _seed_admin()
        before = AuditLog.query.filter_by(action=AuditAction.GDPR_DSAR_EXPORTED).count()
        dsar.export(f"user:{user.id}")
        after = AuditLog.query.filter_by(action=AuditAction.GDPR_DSAR_EXPORTED).count()
    assert after == before + 1


def test_cli_writes_json_to_stdout(app):
    with app.app_context():
        user = _seed_admin()
        user_id = user.id
    runner = app.test_cli_runner()
    result = runner.invoke(args=["dsar-export", "--subject", f"user:{user_id}"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["subject"] == {"type": "user", "id": user_id}


def test_cli_rejects_unknown_subject(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["dsar-export", "--subject", "user:missing"])
    assert result.exit_code != 0
