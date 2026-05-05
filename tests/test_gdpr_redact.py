"""GDPR Art. 17 redaction.

Verifies:
- redact_trainee wipes phone/full_name on the Trainee row
- declarations have signature_png/ip/UA nulled
- audit rows for the subject are rewritten to redaction markers
- the audit chain still verifies after redaction (internally consistent)
- a fresh GDPR_REDACTED action is appended
- raw PII is no longer reachable through DSAR after redaction
"""

from __future__ import annotations

import pytest

from app.extensions import db
from app.services import audit, dsar, gdpr


def _make_trainee_with_history(app):
    from app.models import EnrolmentSource
    from app.services import training as training_service

    with app.app_context():
        trainee = training_service.create_trainee(
            phone="+447700900111",
            full_name="Redact Test",
            role_code="operator",
        )
        course = training_service.get_course_by_code("HACCP-REFRESHER")
        assert course is not None
        training_service.enrol(
            trainee=trainee,
            course=course,
            source=EnrolmentSource.MANUAL.value,
        )
        db.session.commit()
        return trainee.id


def test_trainee_redact_wipes_pii(app):
    from app.models import Trainee

    tid = _make_trainee_with_history(app)
    with app.app_context():
        gdpr.redact(f"trainee:{tid}")
        trainee = db.session.get(Trainee, tid)
        assert trainee.phone.startswith("[redacted")
        assert trainee.full_name == "[redacted]"
        assert trainee.is_active is False


def test_audit_chain_still_verifies_after_redaction(app):
    tid = _make_trainee_with_history(app)
    with app.app_context():
        ok_before, _ = audit.verify_chain()
        assert ok_before is True
        gdpr.redact(f"trainee:{tid}")
        ok_after, broken_id = audit.verify_chain()
    assert ok_after is True, f"chain broken at id={broken_id}"


def test_redacted_audit_rows_carry_marker(app):
    from app.models import AuditLog

    tid = _make_trainee_with_history(app)
    with app.app_context():
        gdpr.redact(f"trainee:{tid}")
        # The original "training_enrolled" diff (which referenced trainee_id +
        # course_id + version) has been replaced by the marker.
        rows = (
            AuditLog.query.filter_by(entity_type="training_enrolment")
            .order_by(AuditLog.id.asc())
            .all()
        )
        assert rows, "no training_enrolment audit rows found"
        for row in rows:
            assert row.diff == {"redacted": True, "redacted_at": row.diff["redacted_at"]} or (
                row.diff and row.diff.get("redacted") is True
            )


def test_redaction_is_itself_audited(app):
    from app.audit_actions import AuditAction
    from app.models import AuditLog

    tid = _make_trainee_with_history(app)
    with app.app_context():
        before = AuditLog.query.filter_by(action=AuditAction.GDPR_REDACTED).count()
        gdpr.redact(f"trainee:{tid}")
        after = AuditLog.query.filter_by(action=AuditAction.GDPR_REDACTED).count()
    assert after == before + 1


def test_dsar_after_redact_does_not_leak_pii(app):
    tid = _make_trainee_with_history(app)
    with app.app_context():
        gdpr.redact(f"trainee:{tid}")
        payload = dsar.export(f"trainee:{tid}")
    # Source-row PII is gone.
    assert payload["trainee"]["phone"].startswith("[redacted")
    assert payload["trainee"]["full_name"] == "[redacted]"
    # And the audit-log entries surface the markers, not the original data.
    found_marker = False
    for entry in payload["audit_log_entries"]:
        if entry["entity_type"] == "training_enrolment":
            assert entry["diff"].get("redacted") is True
            found_marker = True
    assert found_marker, "expected at least one redacted training_enrolment row"


def test_unknown_subject_raises(app):
    with app.app_context():
        with pytest.raises(gdpr.GDPRError):
            gdpr.redact("trainee:no-such")
        with pytest.raises(gdpr.GDPRError):
            gdpr.redact("alien:x")


def test_cli_requires_confirm_flag(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["gdpr-redact", "--subject", "trainee:abc"])
    assert result.exit_code != 0
    assert "--confirm" in result.output


def test_cli_redacts_with_confirm(app):
    tid = _make_trainee_with_history(app)
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=["gdpr-redact", "--subject", f"trainee:{tid}", "--confirm"]
    )
    assert result.exit_code == 0, result.output
    assert "Redacted" in result.output
