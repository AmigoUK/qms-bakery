"""Data Subject Access Request export — UK GDPR Art. 15 / Art. 20.

Walks every table that holds personal data for a given subject and
returns a structured JSON-serialisable dict. The CLI in app/__init__.py
wraps this in `flask dsar-export --subject {user,trainee}:<id>`.

Subjects we cover:
- `user:<user_id>` — staff with QMS accounts
- `trainee:<trainee_id>` — phone-keyed floor workers

Each export is itself audited so the controller has evidence the
right was honoured.

Excluded from output by design (UK GDPR doesn't require them and
disclosing them would weaken security):
- password_hash (would let an attacker offline-crack the password)
- totp_secret (would expose the encrypted second-factor seed)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

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
from app.services import audit


class DSARError(Exception):
    """Raised for malformed subject specs or missing subjects."""


def _isoformat(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _serialise(model_instance, *, exclude: tuple[str, ...] = ()) -> dict:
    out: dict[str, Any] = {}
    for col in model_instance.__table__.columns:
        if col.name in exclude:
            continue
        out[col.name] = _isoformat(getattr(model_instance, col.name))
    return out


def export_user(user_id: str) -> dict:
    user = db.session.get(User, user_id)
    if user is None:
        raise DSARError(f"user {user_id} not found")

    audit_rows = db.session.execute(
        select(AuditLog).where(
            or_(
                AuditLog.user_id == user.id,
                (AuditLog.entity_type == "user") & (AuditLog.entity_id == user.id),
            )
        ).order_by(AuditLog.id.asc())
    ).scalars().all()

    return {
        "subject": {"type": "user", "id": user.id},
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": _serialise(
            user,
            # password_hash and totp_secret deliberately omitted —
            # they're authentication material, not personal data the
            # subject has a right to.
            exclude=("password_hash", "totp_secret"),
        ),
        "role": (
            {"code": user.role.code, "name_en": user.role.name_en, "name_pl": user.role.name_pl}
            if user.role
            else None
        ),
        "audit_log_entries": [
            {
                "id": row.id,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "occurred_at": _isoformat(row.occurred_at),
                "ip_address": row.ip_address,
                "user_agent": row.user_agent,
                "diff": row.diff,
            }
            for row in audit_rows
        ],
    }


def export_trainee(trainee_id: str) -> dict:
    trainee = db.session.get(Trainee, trainee_id)
    if trainee is None:
        raise DSARError(f"trainee {trainee_id} not found")

    enrolments = db.session.execute(
        select(TrainingEnrolment)
        .where(TrainingEnrolment.trainee_id == trainee.id)
        .order_by(TrainingEnrolment.issued_at.asc())
    ).scalars().all()

    enrolment_ids = [e.id for e in enrolments]
    attempts = (
        db.session.execute(
            select(TrainingAttempt).where(TrainingAttempt.enrolment_id.in_(enrolment_ids))
        )
        .scalars()
        .all()
        if enrolment_ids
        else []
    )
    attempt_ids = [a.id for a in attempts]
    declarations = (
        db.session.execute(
            select(TrainingDeclaration).where(TrainingDeclaration.attempt_id.in_(attempt_ids))
        )
        .scalars()
        .all()
        if attempt_ids
        else []
    )
    certifications = db.session.execute(
        select(TrainingCertification).where(TrainingCertification.trainee_id == trainee.id)
    ).scalars().all()

    audit_rows = db.session.execute(
        select(AuditLog)
        .where(
            (AuditLog.entity_type == "trainee") & (AuditLog.entity_id == trainee.id)
            | (AuditLog.entity_type == "training_enrolment")
            & AuditLog.entity_id.in_(enrolment_ids)
            | (AuditLog.entity_type == "training_attempt")
            & AuditLog.entity_id.in_(attempt_ids)
            | (AuditLog.entity_type == "training_declaration")
            & AuditLog.entity_id.in_([d.id for d in declarations])
            | (AuditLog.entity_type == "training_certification")
            & AuditLog.entity_id.in_([c.id for c in certifications])
        )
        .order_by(AuditLog.id.asc())
    ).scalars().all()

    return {
        "subject": {"type": "trainee", "id": trainee.id},
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "trainee": _serialise(trainee),
        "enrolments": [
            # magic_token excluded — it's an authentication credential,
            # not personal data the subject has a right to under Art. 15.
            _serialise(e, exclude=("magic_token",))
            for e in enrolments
        ],
        "attempts": [_serialise(a) for a in attempts],
        "declarations": [
            # signature_png is bytes — base64-encode if you need to ship
            # it, but the column dump still exposes its presence.
            {
                **_serialise(d, exclude=("signature_png",)),
                "signature_png_present": d.signature_png is not None,
                "signature_png_bytes": (
                    len(d.signature_png) if d.signature_png else 0
                ),
            }
            for d in declarations
        ],
        "certifications": [_serialise(c) for c in certifications],
        "audit_log_entries": [
            {
                "id": row.id,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "occurred_at": _isoformat(row.occurred_at),
                "ip_address": row.ip_address,
                "user_agent": row.user_agent,
                "diff": row.diff,
            }
            for row in audit_rows
        ],
    }


def export(subject_spec: str) -> dict:
    """Parse `user:<id>` or `trainee:<id>` and dispatch."""
    if ":" not in subject_spec:
        raise DSARError("subject must be 'user:<id>' or 'trainee:<id>'")
    kind, _, sid = subject_spec.partition(":")
    if kind == "user":
        result = export_user(sid)
    elif kind == "trainee":
        result = export_trainee(sid)
    else:
        raise DSARError(f"unknown subject kind: {kind!r}")
    audit.record(
        entity_type="dsar",
        entity_id=sid,
        action=AuditAction.GDPR_DSAR_EXPORTED,
        diff={"subject_kind": kind},
    )
    db.session.commit()
    return result
