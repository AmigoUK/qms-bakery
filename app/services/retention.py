"""Retention enforcement — UK GDPR Art. 5(1)(e) storage limitation.

Drops or redacts data once it has outlived its lawful purpose. Run by
`flask retention-sweep` on a cron (daily is fine; the operations are
idempotent).

Defaults follow `docs/compliance/lawful-basis.md`:
- expired/withdrawn `TrainingEnrolment` rows older than the magic-link
  TTL grace period are deleted (token already invalid).
- `TrainingDeclaration.signature_png` / `ip_address` / `user_agent` are
  nulled once the linked certification's `valid_until + 7 years` has
  passed.
- inactive `Trainee` rows that have no live cert and haven't been
  active for >24 months are redacted via `services.gdpr.redact_trainee`.
- `audit_log` rows older than 7 years can be flagged for deletion (the
  default operator-policy is "keep" — partition-drop is the recommended
  Postgres mechanism, not a Python loop). This module only counts them.

Returns a structured summary so the CLI can echo it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.audit_actions import AuditAction
from app.extensions import db
from app.models import (
    AuditLog,
    EnrolmentStatus,
    Trainee,
    TrainingCertification,
    TrainingDeclaration,
    TrainingEnrolment,
)
from app.services import audit
from app.services import gdpr as gdpr_service

# Defaults — overridable via Flask config.
DEFAULT_DECLARATION_RETENTION_YEARS = 7
DEFAULT_AUDIT_RETENTION_YEARS = 7
DEFAULT_TRAINEE_DORMANT_MONTHS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_past(dt: datetime | None) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= _now()


def sweep(*, dry_run: bool = True, config: dict | None = None) -> dict:
    """Walk every retention rule. Returns a counts dict.

    `config` overrides the defaults — caller can pass a Flask app
    config or a custom dict for tests.
    """
    cfg = config or {}
    decl_years = int(
        cfg.get("RETENTION_DECLARATION_YEARS", DEFAULT_DECLARATION_RETENTION_YEARS)
    )
    audit_years = int(
        cfg.get("RETENTION_AUDIT_YEARS", DEFAULT_AUDIT_RETENTION_YEARS)
    )
    dormant_months = int(
        cfg.get("RETENTION_TRAINEE_DORMANT_MONTHS", DEFAULT_TRAINEE_DORMANT_MONTHS)
    )

    summary = {
        "dry_run": dry_run,
        "expired_enrolments_dropped": 0,
        "declarations_pii_redacted": 0,
        "trainees_redacted": 0,
        "audit_rows_eligible_for_partition_drop": 0,
    }

    now = _now()

    # 1. Drop expired/withdrawn enrolments older than 30 days. Token
    #    is long since invalid; row carries no compliance value.
    cutoff_enrolment = now - timedelta(days=30)
    expired_enrolments = db.session.execute(
        select(TrainingEnrolment).where(
            TrainingEnrolment.status.in_(
                (EnrolmentStatus.EXPIRED.value, EnrolmentStatus.WITHDRAWN.value)
            ),
            TrainingEnrolment.expires_at < cutoff_enrolment,
        )
    ).scalars().all()
    summary["expired_enrolments_dropped"] = len(expired_enrolments)
    if not dry_run:
        for enrolment in expired_enrolments:
            db.session.delete(enrolment)

    # 2. Null PII columns on declarations whose linked cert has been
    #    expired for more than `decl_years`.
    decl_cutoff = now - timedelta(days=365 * decl_years)
    stale_declarations = db.session.execute(
        select(TrainingDeclaration)
        .join(
            TrainingCertification,
            TrainingDeclaration.id == TrainingCertification.declaration_id,
        )
        .where(TrainingCertification.valid_until < decl_cutoff)
        .where(
            or_(
                TrainingDeclaration.signature_png.isnot(None),
                TrainingDeclaration.ip_address.isnot(None),
                TrainingDeclaration.user_agent.isnot(None),
            )
        )
    ).scalars().all()
    summary["declarations_pii_redacted"] = len(stale_declarations)
    if not dry_run:
        for decl in stale_declarations:
            decl.signature_png = None
            decl.ip_address = None
            decl.user_agent = None

    # 3. Redact dormant trainees: inactive ≥ dormant_months and no
    #    live cert.
    dormant_cutoff = now - timedelta(days=30 * dormant_months)
    dormant_trainees = db.session.execute(
        select(Trainee).where(
            Trainee.is_active.is_(False),
            Trainee.updated_at < dormant_cutoff,
            ~Trainee.full_name.like("[redacted%"),
        )
    ).scalars().all()
    redact_targets = []
    for trainee in dormant_trainees:
        live = db.session.execute(
            select(TrainingCertification.id)
            .where(TrainingCertification.trainee_id == trainee.id)
            .where(TrainingCertification.valid_until > now)
            .where(TrainingCertification.revoked_at.is_(None))
        ).first()
        if live is not None:
            continue
        redact_targets.append(trainee.id)
    summary["trainees_redacted"] = len(redact_targets)
    if not dry_run:
        for tid in redact_targets:
            gdpr_service.redact_trainee(tid)

    # 4. Count audit_log rows older than the retention horizon. Don't
    #    delete in Python — partition-drop on Postgres is the right
    #    primitive. Surface the number so ops can decide.
    audit_cutoff = now - timedelta(days=365 * audit_years)
    summary["audit_rows_eligible_for_partition_drop"] = (
        db.session.execute(
            select(AuditLog.id).where(AuditLog.occurred_at < audit_cutoff)
        ).rowcount
        if False  # rowcount not implemented for SELECT in SQLAlchemy 2.0
        else len(
            db.session.execute(
                select(AuditLog.id).where(AuditLog.occurred_at < audit_cutoff)
            ).scalars().all()
        )
    )

    if not dry_run:
        audit.record(
            entity_type="retention",
            action=AuditAction.GDPR_REDACTED,
            diff={"summary": dict(summary)},
        )
        db.session.commit()

    return summary
