"""GDPR Article 17 — Right to erasure / redaction.

Redaction strategy
------------------
The `audit_log` table is hash-chained: every row's checksum incorporates
the previous row's checksum and its own diff payload. A naive in-place
delete would break verification on every subsequent row.

Instead we *redact*: the row stays, but the personal-data payload in
`diff` is replaced with a `{"redacted": True, ...}` marker. The chain
is then re-hashed forward from the first redacted row to the tail. The
chain remains internally verifiable; an external observer can see that
the chain was redacted at a specific moment but not what was removed.

This is the standard pattern for ledger-style stores under GDPR — the
controller's defence under Art. 17(3)(b) is the legal-obligation
carve-out for keeping the *fact* of an audit event; the personal-data
payload itself enjoys no such protection and is rewritten.

Redaction also nulls the live PII columns on the source rows (Trainee,
TrainingDeclaration). User rows are left intact during the employment +
audit horizon — UK employment-records law overrides Art. 17 for
those.

The redaction action itself is appended to the audit chain so a future
auditor sees both the marker rows AND when redaction happened.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select, text

from app.audit_actions import AuditAction
from app.extensions import db
from app.models import (
    AuditLog,
    Trainee,
    TrainingAttempt,
    TrainingCertification,
    TrainingDeclaration,
    TrainingEnrolment,
    User,
)
from app.models.audit import GENESIS_HASH
from app.services import audit


class GDPRError(Exception):
    """Raised for malformed subject specs or refused redactions."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _redact_audit_rows(audit_ids: list[int]) -> int:
    """Mark each row's diff redacted and rehash the chain forward."""
    if not audit_ids:
        return 0
    bind = db.session.get_bind()
    if bind.dialect.name == "postgresql":
        # Opt this transaction in to bypass the audit_log_immutable_trg
        # trigger. Scope is the current transaction only — any other
        # connection trying to mutate audit_log still gets rejected.
        db.session.execute(text("SET LOCAL qms.audit_redact_ok = '1'"))
    audit_id_set = set(audit_ids)
    first_id = min(audit_ids)
    rows = db.session.execute(
        select(AuditLog).where(AuditLog.id >= first_id).order_by(AuditLog.id.asc())
    ).scalars().all()

    if not rows or rows[0].id != first_id:
        return 0

    # Establish the prev_checksum for the very first row we touch from
    # the row immediately preceding it; if there isn't one, it's the
    # genesis chain start.
    preceding = db.session.execute(
        select(AuditLog).where(AuditLog.id < first_id).order_by(AuditLog.id.desc()).limit(1)
    ).scalar_one_or_none()
    expected_prev = preceding.checksum if preceding else GENESIS_HASH

    redacted_count = 0
    redaction_marker = {"redacted": True, "redacted_at": _now_iso()}
    for row in rows:
        if row.id in audit_id_set:
            row.diff = dict(redaction_marker)
            redacted_count += 1
        # Even rows we don't redact must rehash because their
        # `prev_checksum` chains to a row whose checksum will change.
        row.prev_checksum = expected_prev
        row.checksum = row.compute_checksum()
        expected_prev = row.checksum
    return redacted_count


def redact_trainee(trainee_id: str) -> dict:
    """Redact a trainee's personal data:
    - phone, full_name on the Trainee row → markers
    - signature_png, ip_address, user_agent on every TrainingDeclaration
      linked through the trainee's enrolments → null
    - audit_log diffs for the chain of trainee/enrolment/attempt/decl/cert → marker
    Certifications themselves stay; their dates and pass/fail outcome
    are compliance evidence.
    """
    trainee = db.session.get(Trainee, trainee_id)
    if trainee is None:
        raise GDPRError(f"trainee {trainee_id} not found")

    enrolments = db.session.execute(
        select(TrainingEnrolment).where(TrainingEnrolment.trainee_id == trainee.id)
    ).scalars().all()
    enrolment_ids = [e.id for e in enrolments]
    attempts = (
        db.session.execute(
            select(TrainingAttempt).where(TrainingAttempt.enrolment_id.in_(enrolment_ids))
        ).scalars().all()
        if enrolment_ids
        else []
    )
    attempt_ids = [a.id for a in attempts]
    declarations = (
        db.session.execute(
            select(TrainingDeclaration).where(TrainingDeclaration.attempt_id.in_(attempt_ids))
        ).scalars().all()
        if attempt_ids
        else []
    )
    certifications = db.session.execute(
        select(TrainingCertification).where(TrainingCertification.trainee_id == trainee.id)
    ).scalars().all()

    # 1. Wipe live PII off the source rows.
    declarations_redacted = 0
    for decl in declarations:
        decl.signature_png = None
        decl.ip_address = None
        decl.user_agent = None
        decl.typed_name = "[redacted]"
        declarations_redacted += 1
    trainee.phone = f"[redacted-{trainee.id[:8]}]"
    trainee.full_name = "[redacted]"
    trainee.is_active = False

    # 2. Find every audit row touching this subject's chain and mark them.
    audit_rows = db.session.execute(
        select(AuditLog.id).where(
            or_(
                (AuditLog.entity_type == "trainee") & (AuditLog.entity_id == trainee.id),
                (AuditLog.entity_type == "training_enrolment")
                & AuditLog.entity_id.in_(enrolment_ids),
                (AuditLog.entity_type == "training_attempt")
                & AuditLog.entity_id.in_(attempt_ids),
                (AuditLog.entity_type == "training_declaration")
                & AuditLog.entity_id.in_([d.id for d in declarations]),
                (AuditLog.entity_type == "training_certification")
                & AuditLog.entity_id.in_([c.id for c in certifications]),
            )
        ).order_by(AuditLog.id.asc())
    ).scalars().all()
    audit_ids = list(audit_rows)
    redacted_audit = _redact_audit_rows(audit_ids)
    db.session.flush()

    # 3. Append a fresh audit entry recording the redaction itself.
    audit.record(
        entity_type="gdpr",
        entity_id=trainee.id,
        action=AuditAction.GDPR_REDACTED,
        diff={
            "subject_kind": "trainee",
            "audit_rows_redacted": redacted_audit,
            "declarations_redacted": declarations_redacted,
        },
    )
    db.session.commit()

    return {
        "subject": {"type": "trainee", "id": trainee.id},
        "audit_rows_redacted": redacted_audit,
        "declarations_redacted": declarations_redacted,
        "redacted_at": _now_iso(),
    }


def redact_user(user_id: str) -> dict:
    """Redact a user's personal data from audit_log diffs, but leave
    the User row intact (employment record requirement)."""
    user = db.session.get(User, user_id)
    if user is None:
        raise GDPRError(f"user {user_id} not found")

    audit_ids = list(
        db.session.execute(
            select(AuditLog.id).where(
                or_(
                    AuditLog.user_id == user.id,
                    (AuditLog.entity_type == "user") & (AuditLog.entity_id == user.id),
                )
            ).order_by(AuditLog.id.asc())
        ).scalars().all()
    )
    redacted_audit = _redact_audit_rows(audit_ids)
    db.session.flush()

    audit.record(
        entity_type="gdpr",
        entity_id=user.id,
        action=AuditAction.GDPR_REDACTED,
        diff={
            "subject_kind": "user",
            "audit_rows_redacted": redacted_audit,
            "note": "user row retained for employment-record obligation",
        },
    )
    db.session.commit()

    return {
        "subject": {"type": "user", "id": user.id},
        "audit_rows_redacted": redacted_audit,
        "redacted_at": _now_iso(),
    }


def redact(subject_spec: str) -> dict:
    if ":" not in subject_spec:
        raise GDPRError("subject must be 'user:<id>' or 'trainee:<id>'")
    kind, _, sid = subject_spec.partition(":")
    if kind == "user":
        return redact_user(sid)
    if kind == "trainee":
        return redact_trainee(sid)
    raise GDPRError(f"unknown subject kind: {kind!r}")
