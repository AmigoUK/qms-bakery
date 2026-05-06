"""Training domain service: enrol → take → score → declare → certify.

Public API
----------
- `create_trainee(...)`           — admin creates a worker
- `create_course(code)`           — admin creates a course shell
- `add_course_version(...)`       — admin saves a new immutable version
- `enrol(trainee, course, *, source, source_ref)` — issues an enrolment
  + magic-link token; enqueues the SMS via the existing ClickSend queue
- `start_attempt(enrolment)`      — moves enrolment to STARTED
- `submit_attempt(enrolment, answers)` — scores against the version's
  answer key, freezes the response payload, returns the Attempt
- `record_declaration(...)`       — captures typed name + signature blob;
  if the attempt passed, also issues a Certification
- `is_due_for_recert(trainee, course)` — for the scheduler
"""

from __future__ import annotations

import calendar
import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Iterable

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.audit_actions import AuditAction
from app.extensions import db
from app.models import (
    EnrolmentSource,
    EnrolmentStatus,
    NotificationChannel,
    Trainee,
    TrainingAssignment,
    TrainingAttempt,
    TrainingCertification,
    TrainingCourse,
    TrainingCourseVersion,
    TrainingDeclaration,
    TrainingEnrolment,
)
from app.services import audit, training_links
from app.services.queue import enqueue_email, enqueue_sms


class TrainingError(Exception):
    """Domain error from the training service."""


class ComplianceState(enum.StrEnum):
    """Per-cell state in the trainee × course compliance matrix.

    Single source of truth for both the admin dashboard colours and
    the worker-clearance aggregate. Six states ordered roughly by
    operational urgency:

    - NOT_REQUIRED — no TrainingAssignment matches this trainee for
      this course; not part of their compliance profile.
    - EXTRA       — not required, but the trainee happens to hold a
      valid cert (e.g. previous role, voluntary). Shown lightly so
      it doesn't compete visually with required cells.
    - VALID       — required + valid cert + outside the recert lead
      window. The "everything is fine" state.
    - DUE_SOON    — required + valid cert, but inside the lead window
      (default 14 days before valid_until). Operator should plan
      issuance soon.
    - IN_FLIGHT   — required + a magic-link enrolment is already
      issued/started (recert in progress). Don't issue another.
    - OVERDUE     — required + cert expired or never issued and no
      enrolment in flight. Worker is NOT cleared for work.
    """

    NOT_REQUIRED = "not_required"
    EXTRA = "extra"
    VALID = "valid"
    DUE_SOON = "due_soon"
    IN_FLIGHT = "in_flight"
    OVERDUE = "overdue"


# States that block worker clearance: any single OVERDUE → not cleared.
_BLOCKING_STATES: frozenset[ComplianceState] = frozenset(
    {ComplianceState.OVERDUE}
)


def _assignment_matches(assignment: TrainingAssignment, trainee: Trainee) -> bool:
    """True if the assignment's role/line predicate matches this trainee.

    Wildcard semantics: a NULL `role_code` or `line_id` on the
    assignment matches any value. Both NULL = applies to everyone.
    Mirrors the matching rule already used by the recurrence
    scheduler in `trainees_due_for_course` (kept in sync via this
    shared helper).
    """
    if assignment.role_code is not None and assignment.role_code != trainee.role_code:
        return False
    if assignment.line_id is not None and assignment.line_id != trainee.line_id:
        return False
    return True


def compliance_state(
    *,
    trainee: Trainee,
    course: TrainingCourse,
    is_required: bool,
    cert: TrainingCertification | None,
    has_in_flight: bool,
) -> ComplianceState:
    """Resolve a single trainee × course cell to a ComplianceState.

    Caller pre-loads `is_required`, `cert`, `has_in_flight` (typically
    from `build_compliance_matrix`) so this function does no I/O. That
    keeps the matrix loader at O(1) queries for the whole grid.

    The trainee/course parameters are accepted for symmetry with the
    bulk-loader and to support future per-trainee overrides; they
    aren't consulted in the current logic.
    """
    del trainee, course  # unused — symmetry only; flagged so linters don't squawk.

    # An open enrolment is an in-progress recert/issuance and is worth
    # surfacing whether or not the course is required — the plant
    # manager still cares that activity is happening.
    if has_in_flight:
        return ComplianceState.IN_FLIGHT
    cert_valid = cert is not None and not _is_past(cert.valid_until)
    if not is_required:
        return ComplianceState.EXTRA if cert_valid else ComplianceState.NOT_REQUIRED
    if cert is None or _is_past(cert.valid_until):
        return ComplianceState.OVERDUE
    lead_days = current_app.config.get("TRAINING_RECERT_LEAD_DAYS", 14)
    if _is_past(cert.valid_until - timedelta(days=lead_days)):
        return ComplianceState.DUE_SOON
    return ComplianceState.VALID


def _now() -> datetime:
    return datetime.now(UTC)


def _is_past(dt: datetime) -> bool:
    """Compare a possibly-naive `dt` (e.g. from SQLite read-back, which
    drops tzinfo) against now. Naive values are treated as UTC.
    PG preserves tzinfo correctly; this is a portability shim."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt <= _now()


def _add_months(dt: datetime, months: int) -> datetime:
    """Add `months` calendar months to dt, clamping day to month length.
    e.g. (2026-01-31, +1) → 2026-02-28; (2026-02-29-leap, +12) → 2027-02-28.
    Avoids the 30-day-month drift that timedelta(days=30*months) introduces
    over multi-year cert lifetimes."""
    total = dt.month - 1 + months
    year = dt.year + total // 12
    month = total % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


# ─── Trainee + course CRUD ─────────────────────────────────────────


def create_trainee(
    *,
    phone: str,
    full_name: str,
    role_code: str,
    line_id: str | None = None,
    language: str = "en",
    email: str | None = None,
    notification_channel: str = NotificationChannel.SMS.value,
    employee_number: str | None = None,
) -> Trainee:
    if not phone or not full_name or not role_code:
        raise TrainingError("phone, full_name, role_code are required")
    if Trainee.query.filter_by(phone=phone).first():
        raise TrainingError(f"phone {phone} already registered")
    if notification_channel not in {c.value for c in NotificationChannel}:
        raise TrainingError(
            f"unknown notification_channel {notification_channel!r}"
        )
    if employee_number:
        employee_number = employee_number.strip() or None
    if employee_number and Trainee.query.filter_by(employee_number=employee_number).first():
        raise TrainingError(
            f"employee_number {employee_number!r} already registered"
        )
    trainee = Trainee(
        employee_number=employee_number,
        phone=phone,
        email=(email or None),
        full_name=full_name,
        role_code=role_code,
        line_id=line_id,
        language=language,
        notification_channel=notification_channel,
    )
    db.session.add(trainee)
    db.session.flush()
    audit.record(
        entity_type="trainee",
        entity_id=trainee.id,
        action=AuditAction.CREATE,
        # Phone is on the Trainee row (reachable via entity_id) and
        # also redacted-for-life if the operator runs gdpr-redact.
        # Logging only the role_code keeps the audit chain free of PII.
        diff={"role_code": role_code},
    )
    return trainee


def get_course_by_code(code: str) -> TrainingCourse | None:
    return TrainingCourse.query.filter_by(code=code).first()


def create_course(*, code: str, description: str | None = None) -> TrainingCourse:
    if get_course_by_code(code):
        raise TrainingError(f"course {code} already exists")
    course = TrainingCourse(code=code, description=description)
    db.session.add(course)
    db.session.flush()
    audit.record(
        entity_type="training_course",
        entity_id=course.id,
        action=AuditAction.CREATE,
        diff={"code": code},
    )
    return course


def add_course_version(
    *,
    course: TrainingCourse,
    title: dict,
    summary: dict | None = None,
    pass_threshold: float | None = None,
    validity_months: int | None = None,
    link_ttl_days: int | None = None,
    exam_time_limit_seconds: int | None = None,
) -> TrainingCourseVersion:
    """Create the next version of a course. Older versions stay (with
    is_active=False) so in-flight enrolments remain valid."""
    cfg = current_app.config
    next_version = 1 + max((v.version for v in course.versions), default=0)
    for v in course.versions:
        v.is_active = False
    new_version = TrainingCourseVersion(
        course_id=course.id,
        version=next_version,
        is_active=True,
        title=title,
        summary=summary or {"pl": "", "en": ""},
        pass_threshold=(
            pass_threshold
            if pass_threshold is not None
            else cfg.get("TRAINING_DEFAULT_PASS_THRESHOLD", 0.7)
        ),
        validity_months=validity_months or 12,
        link_ttl_days=(
            link_ttl_days
            if link_ttl_days is not None
            else cfg.get("TRAINING_DEFAULT_LINK_TTL_DAYS", 7)
        ),
        exam_time_limit_seconds=exam_time_limit_seconds,
    )
    db.session.add(new_version)
    db.session.flush()
    audit.record(
        entity_type="training_course",
        entity_id=course.id,
        action=AuditAction.CREATE_VERSION,
        diff={"version": next_version},
    )
    return new_version


# ─── Enrolment + magic link ─────────────────────────────────────────


def enrol(
    *,
    trainee: Trainee,
    course: TrainingCourse,
    source: str = EnrolmentSource.MANUAL.value,
    source_ref: str | None = None,
    base_url: str | None = None,
) -> TrainingEnrolment:
    """Create an Enrolment, sign a magic-link token, enqueue the SMS.

    `base_url` is the public hostname for the link (e.g. `https://qms.example.com`).
    Falls back to app.config["TRAINING_BASE_URL"] or omits the host (relative URL)
    if neither is set — the SMS body would then need a separate prefix.
    """
    version = course.active_version
    if version is None:
        raise TrainingError(f"course {course.code} has no active version")

    issued_at = _now()
    expires_at = issued_at + timedelta(days=version.link_ttl_days)
    enrolment = TrainingEnrolment(
        trainee_id=trainee.id,
        course_version_id=version.id,
        magic_token="",  # set after we have an id
        issued_at=issued_at,
        expires_at=expires_at,
        status=EnrolmentStatus.ISSUED.value,
        source=source,
        source_ref=source_ref,
    )
    db.session.add(enrolment)
    db.session.flush()
    enrolment.magic_token = training_links.issue_token(enrolment.id, expires_at)
    db.session.flush()

    audit.record(
        entity_type="training_enrolment",
        entity_id=enrolment.id,
        action=AuditAction.TRAINING_ENROLLED,
        diff={
            "trainee_id": trainee.id,
            "course_id": course.id,
            "version": version.version,
            "source": source,
        },
    )

    base = base_url or current_app.config.get("TRAINING_BASE_URL", "")
    link = f"{base.rstrip('/')}/training/take/{enrolment.magic_token}"
    title_en = (version.title or {}).get("en") or course.code
    body = f"{title_en} training: {link} (expires {expires_at:%Y-%m-%d})"

    delivery = _deliver_magic_link(
        trainee=trainee,
        title=title_en,
        link=link,
        body=body,
        expires_at=expires_at,
        enrolment_id=enrolment.id,
    )
    audit.record(
        entity_type="training_enrolment",
        entity_id=enrolment.id,
        action=AuditAction.TRAINING_LINK_SENT,
        diff={
            "phone_suffix": trainee.phone[-4:],
            "channel_requested": trainee.notification_channel,
            **delivery,
        },
    )
    return enrolment


def _deliver_magic_link(
    *,
    trainee: Trainee,
    title: str,
    link: str,
    body: str,
    expires_at: datetime,
    enrolment_id: str,
) -> dict[str, bool]:
    """Channel-aware delivery for the magic-link.

    Returns a small dict (`sms_queued`, `email_queued`) so the audit
    record can capture which channels actually fired.

    Fallback rules:
    - channel=EMAIL but trainee has no email → fall back to SMS, log warning.
    - channel=BOTH with no email → SMS only (no warning, plain BOTH-without-email).
    - All enqueues are best-effort: a Redis outage doesn't 500 the
      enrolment (consistent with the existing pattern).
    """
    channel = trainee.notification_channel
    has_email = bool((trainee.email or "").strip())

    want_sms = channel in (
        NotificationChannel.SMS.value,
        NotificationChannel.BOTH.value,
    )
    want_email = channel in (
        NotificationChannel.EMAIL.value,
        NotificationChannel.BOTH.value,
    )

    if want_email and not has_email:
        current_app.logger.warning(
            "training.enrol: channel=%s requested for enrolment %s but "
            "trainee has no email on file — falling back to SMS",
            channel,
            enrolment_id,
        )
        want_email = False
        want_sms = True

    sms_queued = False
    if want_sms:
        try:
            enqueue_sms(to=trainee.phone, body=body)
            sms_queued = True
        except Exception as exc:
            current_app.logger.warning(
                "training.enrol: SMS enqueue failed for enrolment %s — "
                "link still issued, manual delivery required: %s",
                enrolment_id,
                exc,
            )

    email_queued = False
    if want_email:
        subject = f"[QMS] {title}"
        text = (
            f"{body}\n\n"
            f"Open the link on your phone to begin. "
            f"This link expires on {expires_at:%Y-%m-%d}.\n"
        )
        html = (
            f"<p>{title} training link</p>"
            f'<p><a href="{link}">{link}</a></p>'
            f"<p>This link expires on {expires_at:%Y-%m-%d}.</p>"
        )
        try:
            enqueue_email(to=trainee.email, subject=subject, body_text=text, body_html=html)
            email_queued = True
        except Exception as exc:
            current_app.logger.warning(
                "training.enrol: email enqueue failed for enrolment %s: %s",
                enrolment_id,
                exc,
            )

    return {"sms_queued": sms_queued, "email_queued": email_queued}


def get_enrolment_by_token(token: str) -> TrainingEnrolment | None:
    """Verify the token and return the enrolment if it's still live.

    Returns None when the token is malformed/expired/forged or when the
    stored expires_at is past — same negative result for all three so we
    don't leak which case applied.
    """
    enrolment, _reason = get_enrolment_with_reason(token)
    return enrolment


def get_enrolment_with_reason(
    token: str,
) -> tuple[TrainingEnrolment | None, str]:
    """Like `get_enrolment_by_token` but also returns *why* it failed.

    Returns `(enrolment, "")` on success, or `(None, reason)` where
    reason is one of:
      - "expired" — TTL past (signed token's exp_unix is in the past,
        or the row's stored expires_at is past, or the row's token has
        been superseded by a re-enrol)
      - "invalid" — token shape is wrong, signature doesn't verify, or
        the row referenced by the token doesn't exist

    Trainees see different copy on the 410 page based on this reason
    so they know whether to ask for a fresh link (expired) or check
    that they tapped the SMS in full (invalid). The classification is
    coarse on purpose: we don't distinguish "real row was re-enrolled"
    from "expired" because both have the same recovery action, and
    we don't distinguish "bad signature" from "bad shape" because
    both need the same fix.
    """
    try:
        enrolment_id = training_links.verify_token(token)
    except training_links.InvalidToken as exc:
        return (None, "expired" if "expired" in str(exc) else "invalid")
    enrolment = db.session.get(TrainingEnrolment, enrolment_id)
    if enrolment is None:
        return (None, "invalid")
    if enrolment.magic_token != token:
        return (None, "expired")
    if _is_past(enrolment.expires_at):
        return (None, "expired")
    return (enrolment, "")


# ─── Attempt lifecycle ──────────────────────────────────────────────


def start_attempt(enrolment: TrainingEnrolment) -> TrainingAttempt:
    """Idempotent — calling twice returns the same Attempt."""
    existing = TrainingAttempt.query.filter_by(enrolment_id=enrolment.id).first()
    if existing is not None:
        return existing
    enrolment.status = EnrolmentStatus.STARTED.value
    attempt = TrainingAttempt(
        enrolment_id=enrolment.id,
        started_at=_now(),
    )
    db.session.add(attempt)
    db.session.flush()
    audit.record(
        entity_type="training_attempt",
        entity_id=attempt.id,
        action=AuditAction.TRAINING_ATTEMPT_STARTED,
    )
    return attempt


def submit_attempt(
    enrolment: TrainingEnrolment, answers: dict[str, list[str]]
) -> TrainingAttempt:
    """Score the answers; freeze the response snapshot.

    `answers` maps question_id → list of selected option_ids. Single-choice
    just gets a one-element list. We score by exact set-equality with the
    correct option set.
    """
    attempt = (
        TrainingAttempt.query.filter_by(enrolment_id=enrolment.id).first()
        or start_attempt(enrolment)
    )
    if attempt.submitted_at is not None:
        raise TrainingError("attempt already submitted")

    version = enrolment.course_version
    questions = version.questions
    if not questions:
        raise TrainingError("course version has no questions")

    correct_count = 0
    snapshot: list[dict] = []
    for q in questions:
        correct_ids = {o.id for o in q.options if o.is_correct}
        if not correct_ids:
            # The admin authoring UI rejects this at save time, but a
            # question that lost its only correct option through some
            # other path would otherwise silently make a course
            # un-passable. Surface it in logs so ops can spot it.
            current_app.logger.warning(
                "training.score: question %s on course_version %s has no "
                "correct options — trainees cannot answer it correctly",
                q.id,
                version.id,
            )
        selected = set(answers.get(q.id, []))
        is_right = selected == correct_ids and len(correct_ids) > 0
        if is_right:
            correct_count += 1
        snapshot.append(
            {
                "question_id": q.id,
                "selected_option_ids": sorted(selected),
                "is_correct": is_right,
            }
        )

    score = correct_count / len(questions)
    passed = score >= version.pass_threshold

    attempt.submitted_at = _now()
    attempt.score = score
    attempt.passed = passed
    attempt.payload = {"answers": snapshot, "n_questions": len(questions)}
    enrolment.status = EnrolmentStatus.SUBMITTED.value
    db.session.flush()

    audit.record(
        entity_type="training_attempt",
        entity_id=attempt.id,
        action=AuditAction.TRAINING_ATTEMPT_SUBMITTED,
        diff={"score": round(score, 4), "passed": passed},
    )
    return attempt


def record_declaration(
    *,
    attempt: TrainingAttempt,
    typed_name: str,
    declaration_text: str,
    signature_png: bytes | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> TrainingDeclaration:
    """Persist the declaration and, if the attempt passed, the cert."""
    if attempt.submitted_at is None:
        raise TrainingError("attempt not yet submitted")
    if not typed_name.strip():
        raise TrainingError("typed_name is required")
    existing = TrainingDeclaration.query.filter_by(attempt_id=attempt.id).first()
    if existing is not None:
        raise TrainingError("declaration already recorded")

    decl = TrainingDeclaration(
        attempt_id=attempt.id,
        typed_name=typed_name.strip(),
        declaration_text=declaration_text,
        signature_png=signature_png,
        ip_address=ip_address,
        user_agent=user_agent,
        signed_at=_now(),
    )
    db.session.add(decl)
    db.session.flush()
    audit.record(
        entity_type="training_declaration",
        entity_id=decl.id,
        action=AuditAction.TRAINING_DECLARED,
        diff={"typed_name": decl.typed_name, "has_signature": signature_png is not None},
    )

    if attempt.passed:
        enrolment = db.session.get(TrainingEnrolment, attempt.enrolment_id)
        version = db.session.get(TrainingCourseVersion, enrolment.course_version_id)
        valid_from = _now()
        valid_until = _add_months(valid_from, version.validity_months)
        cert = TrainingCertification(
            trainee_id=enrolment.trainee_id,
            course_id=version.course_id,
            course_version_id=version.id,
            attempt_id=attempt.id,
            declaration_id=decl.id,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        db.session.add(cert)
        db.session.flush()
        audit.record(
            entity_type="training_certification",
            entity_id=cert.id,
            action=AuditAction.TRAINING_CERTIFIED,
            diff={
                "course_id": version.course_id,
                "version": version.version,
                "valid_until": valid_until.isoformat(),
            },
        )

    return decl


# ─── Recurrence detection ──────────────────────────────────────────


def latest_certification(
    trainee_id: str, course_id: str
) -> TrainingCertification | None:
    return (
        db.session.execute(
            select(TrainingCertification)
            .where(TrainingCertification.trainee_id == trainee_id)
            .where(TrainingCertification.course_id == course_id)
            .where(TrainingCertification.revoked_at.is_(None))
            .order_by(TrainingCertification.valid_until.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def has_open_enrolment(trainee_id: str, course_id: str) -> bool:
    """True if the trainee already has a non-expired, non-submitted
    enrolment for any version of this course — used by the scheduler
    for idempotency so a restart doesn't double-issue SMSes."""
    open_states = (EnrolmentStatus.ISSUED.value, EnrolmentStatus.STARTED.value)
    rows = (
        db.session.execute(
            select(TrainingEnrolment)
            .join(
                TrainingCourseVersion,
                TrainingEnrolment.course_version_id == TrainingCourseVersion.id,
            )
            .where(TrainingCourseVersion.course_id == course_id)
            .where(TrainingEnrolment.trainee_id == trainee_id)
            .where(TrainingEnrolment.status.in_(open_states))
            # SQLAlchemy renders tz-aware datetime correctly for both
            # PG and SQLite at the SQL layer — this comparison is fine
            # in the query even though Python-side reads strip tzinfo.
            .where(TrainingEnrolment.expires_at > _now())
        )
        .scalars()
        .all()
    )
    return bool(rows)


def is_due_for_recert(trainee: Trainee, course: TrainingCourse) -> bool:
    """True when trainee should be re-issued an SMS for this course.

    Due if: there is no live cert (never trained, or cert is within the
    lead window before expiry, or already expired) AND no open enrolment
    in flight. The lead window (TRAINING_RECERT_LEAD_DAYS, default 14)
    closes the SMS-roundtrip + retake gap so a worker isn't briefly
    non-compliant between cert expiry and the next pass.
    """
    if has_open_enrolment(trainee.id, course.id):
        return False
    cert = latest_certification(trainee.id, course.id)
    if cert is None:
        return True
    lead_days = current_app.config.get("TRAINING_RECERT_LEAD_DAYS", 14)
    threshold = cert.valid_until - timedelta(days=lead_days)
    return _is_past(threshold)


def trainees_due_for_course(course: TrainingCourse) -> Iterable[Trainee]:
    """Yield active trainees who match any of the course's assignments
    (role/line) AND are due for re-cert."""
    course = db.session.get(TrainingCourse, course.id)
    if course is None or not course.assignments:
        return []
    out: list[Trainee] = []
    seen: set[str] = set()
    for trainee in Trainee.query.filter_by(is_active=True).all():
        for a in course.assignments:
            if not _assignment_matches(a, trainee):
                continue
            if trainee.id in seen:
                continue
            if is_due_for_recert(trainee, course):
                out.append(trainee)
                seen.add(trainee.id)
            break
    return out


# ─── Compliance matrix bulk-loader ─────────────────────────────────


def build_compliance_matrix(
    *,
    line_id: str | None = None,
    role_code: str | None = None,
    line_ids: list[str] | None = None,
    role_codes: list[str] | None = None,
    blocked_only: bool = False,
) -> dict:
    """Bulk-loaded snapshot for the admin compliance dashboard.

    Optional filters narrow the visible trainee set:

    - `line_ids` — restrict to these production lines (multi-select)
    - `role_codes` — restrict to these roles (multi-select)
    - `line_id` / `role_code` — singleton convenience aliases for the
      common case; combined with the list parameters when both are
      provided
    - `blocked_only` — drop fully-cleared trainees (useful when an
      operator wants to see "who can't be put on shift today?")

    Filters apply after the bulk-load so the loader still hits the
    DB ≤ 5 times; KPIs are then computed over the filtered subset
    so the strip and matrix stay consistent.

    Issues at most five queries regardless of grid size, then
    dispatches every (trainee × course) pair through
    `compliance_state(...)` in pure Python. The naive per-cell
    approach (`latest_certification` + `is_due_for_recert` per cell)
    fans out to thousands of queries on a real deployment.

    Returns a dict the dashboard view can pass straight to its
    template:

      {
        "trainees": [Trainee, ...],
        "courses":  [TrainingCourse, ...],
        "states":   {(trainee_id, course_id): ComplianceState, ...},
        "certs":    {(trainee_id, course_id): TrainingCertification, ...},
        "cleared":  {trainee_id: bool, ...},
        "kpis": {
          "blocked":         int,  # number of workers not cleared
          "overdue_courses": int,  # number of distinct courses with at least one OVERDUE
          "due_soon":        int,  # number of (trainee × course) cells DUE_SOON
          "in_flight":       int,  # number of cells IN_FLIGHT
        },
      }
    """
    # 1. Trainees (active only). Apply line / role filters at the SQL
    # level so we don't load workers we'll throw away. Singleton +
    # list inputs combine into one set each — duplicates collapse.
    line_set: set[str] = set()
    if line_id:
        line_set.add(line_id)
    if line_ids:
        line_set.update(x for x in line_ids if x)
    role_set: set[str] = set()
    if role_code:
        role_set.add(role_code)
    if role_codes:
        role_set.update(x for x in role_codes if x)

    trainee_q = Trainee.query.filter_by(is_active=True)
    if line_set:
        trainee_q = trainee_q.filter(Trainee.line_id.in_(line_set))
    if role_set:
        trainee_q = trainee_q.filter(Trainee.role_code.in_(role_set))
    trainees = trainee_q.order_by(Trainee.full_name).all()
    # 2. Courses + their assignments (eager-loaded to avoid lazy fan-out).
    courses = (
        TrainingCourse.query.filter_by(is_active=True)
        .options(selectinload(TrainingCourse.assignments))
        .order_by(TrainingCourse.code)
        .all()
    )

    if not trainees or not courses:
        return {
            "trainees": trainees,
            "courses": courses,
            "states": {},
            "certs": {},
            "cleared": {t.id: True for t in trainees},
            "kpis": {"blocked": 0, "overdue_courses": 0, "due_soon": 0, "in_flight": 0},
        }

    trainee_ids = [t.id for t in trainees]
    course_ids = [c.id for c in courses]

    # 3. Latest non-revoked cert per (trainee, course). Sort ascending
    # so the dict assignment "last wins" yields the most recent.
    cert_rows = (
        TrainingCertification.query.filter(
            TrainingCertification.trainee_id.in_(trainee_ids),
            TrainingCertification.course_id.in_(course_ids),
            TrainingCertification.revoked_at.is_(None),
        )
        .order_by(TrainingCertification.valid_until.asc())
        .all()
    )
    cert_by: dict[tuple[str, str], TrainingCertification] = {}
    for c in cert_rows:
        cert_by[(c.trainee_id, c.course_id)] = c

    # 4. Open enrolments (issued/started, not expired). Project to
    # (trainee_id, course_id) via course_version → course join, then
    # collapse to a set for O(1) `in_flight` lookups.
    open_states = (EnrolmentStatus.ISSUED.value, EnrolmentStatus.STARTED.value)
    open_pairs: set[tuple[str, str]] = set()
    enrolment_rows = (
        db.session.execute(
            select(
                TrainingEnrolment.trainee_id,
                TrainingCourseVersion.course_id,
            )
            .join(
                TrainingCourseVersion,
                TrainingEnrolment.course_version_id == TrainingCourseVersion.id,
            )
            .where(TrainingEnrolment.trainee_id.in_(trainee_ids))
            .where(TrainingCourseVersion.course_id.in_(course_ids))
            .where(TrainingEnrolment.status.in_(open_states))
            .where(TrainingEnrolment.expires_at > _now())
        )
        .all()
    )
    for tid, cid in enrolment_rows:
        open_pairs.add((tid, cid))

    # 5. Required-flags from the eager-loaded assignments — no I/O.
    required: dict[tuple[str, str], bool] = {}
    for course in courses:
        assignments = list(course.assignments)
        for trainee in trainees:
            required[(trainee.id, course.id)] = any(
                _assignment_matches(a, trainee) for a in assignments
            )

    # Resolve each cell.
    states: dict[tuple[str, str], ComplianceState] = {}
    for course in courses:
        for trainee in trainees:
            key = (trainee.id, course.id)
            states[key] = compliance_state(
                trainee=trainee,
                course=course,
                is_required=required[key],
                cert=cert_by.get(key),
                has_in_flight=key in open_pairs,
            )

    # Worker clearance: blocked iff any required cell is OVERDUE.
    cleared: dict[str, bool] = {}
    for trainee in trainees:
        cleared[trainee.id] = not any(
            states[(trainee.id, course.id)] in _BLOCKING_STATES for course in courses
        )

    # `blocked_only`: drop fully-cleared trainees from the visible
    # list. KPIs below are computed AFTER this filter so they reflect
    # what the operator actually sees.
    if blocked_only:
        trainees = [t for t in trainees if cleared.get(t.id) is False]
        visible_ids = {t.id for t in trainees}
        states = {k: v for k, v in states.items() if k[0] in visible_ids}
        cleared = {tid: cleared[tid] for tid in visible_ids}
        cert_by = {k: v for k, v in cert_by.items() if k[0] in visible_ids}

    # KPIs reflect the (possibly filtered) view so the strip and
    # matrix tell the same story.
    blocked = sum(1 for v in cleared.values() if v is False)
    due_soon = sum(1 for s in states.values() if s is ComplianceState.DUE_SOON)
    in_flight = sum(1 for s in states.values() if s is ComplianceState.IN_FLIGHT)
    overdue_courses = len(
        {
            cid
            for (_tid, cid), s in states.items()
            if s is ComplianceState.OVERDUE
        }
    )

    return {
        "trainees": trainees,
        "courses": courses,
        "states": states,
        "certs": cert_by,
        "cleared": cleared,
        "kpis": {
            "blocked": blocked,
            "overdue_courses": overdue_courses,
            "due_soon": due_soon,
            "in_flight": in_flight,
        },
    }


# ─── Bulk-issue planner & applier ──────────────────────────────────


@dataclass
class BulkIssueRow:
    """Per-trainee classification produced by `plan_bulk_issue`.

    `trainee` is the matched ORM row when found; None for not-found
    inputs (rare — only happens if the form is replayed against a
    deleted UUID). `note` carries the human-readable detail the
    confirm screen uses (e.g. "active until 2026-05-08")."""

    trainee: Trainee | None
    action: str  # "issue" | "skip_in_flight" | "skip_no_version" | "error"
    note: str = ""


@dataclass
class BulkIssuePlan:
    course: TrainingCourse
    rows: list[BulkIssueRow] = field(default_factory=list)

    @property
    def n_issue(self) -> int:
        return sum(1 for r in self.rows if r.action == "issue")

    @property
    def n_skip(self) -> int:
        return sum(1 for r in self.rows if r.action.startswith("skip"))

    @property
    def n_error(self) -> int:
        return sum(1 for r in self.rows if r.action == "error")


def plan_bulk_issue(
    trainee_ids: Iterable[str], course: TrainingCourse
) -> BulkIssuePlan:
    """Classify each trainee as issue / skip / error without writing
    anything. The view layer renders the result on the confirm screen
    and replays the same input list to apply().

    Skip rules:
    - The course has no active version → all rows are skip_no_version.
    - The trainee already has an open ISSUED/STARTED enrolment for
      this course → skip_in_flight, with the existing expiry as note
      so the operator sees why.
    - Trainee row missing entirely → "error".
    """
    plan = BulkIssuePlan(course=course)
    if course.active_version is None:
        for tid in trainee_ids:
            t = db.session.get(Trainee, tid)
            plan.rows.append(
                BulkIssueRow(
                    trainee=t,
                    action="skip_no_version",
                    note="course has no active version",
                )
            )
        return plan

    open_states = (EnrolmentStatus.ISSUED.value, EnrolmentStatus.STARTED.value)
    for tid in trainee_ids:
        trainee = db.session.get(Trainee, tid)
        if trainee is None:
            plan.rows.append(
                BulkIssueRow(trainee=None, action="error", note=f"trainee {tid} not found")
            )
            continue
        # Look up the live enrolment so the confirm screen can show
        # how long the existing token is still good for.
        existing = db.session.execute(
            select(TrainingEnrolment)
            .join(
                TrainingCourseVersion,
                TrainingEnrolment.course_version_id == TrainingCourseVersion.id,
            )
            .where(TrainingCourseVersion.course_id == course.id)
            .where(TrainingEnrolment.trainee_id == trainee.id)
            .where(TrainingEnrolment.status.in_(open_states))
            .where(TrainingEnrolment.expires_at > _now())
            .order_by(TrainingEnrolment.expires_at.desc())
            .limit(1)
        ).scalars().first()
        if existing is not None:
            plan.rows.append(
                BulkIssueRow(
                    trainee=trainee,
                    action="skip_in_flight",
                    note=f"active until {existing.expires_at:%Y-%m-%d}",
                )
            )
            continue
        plan.rows.append(BulkIssueRow(trainee=trainee, action="issue"))
    return plan


def apply_bulk_issue(
    plan: BulkIssuePlan, *, base_url: str | None = None, by_user_id: str | None = None
) -> dict[str, int]:
    """Issue magic-links for every "issue" row by calling `enrol`.

    Each `enrol` call mints a fresh `TrainingEnrolment` with its own
    UUID and HMAC-signed magic_token, so two trainees in the same
    bulk never share a token even at the byte level.

    Best-effort: a per-row failure is logged + counted as `errors`
    but doesn't abort the rest of the batch. Caller commits.
    """
    counts = {"issued": 0, "skipped": 0, "errors": 0}
    source_ref = f"bulk:{by_user_id or 'unknown'}"
    for row in plan.rows:
        if row.action != "issue" or row.trainee is None:
            counts["skipped"] += 1
            continue
        try:
            enrol(
                trainee=row.trainee,
                course=plan.course,
                source=EnrolmentSource.MANUAL.value,
                source_ref=source_ref,
                base_url=base_url,
            )
            counts["issued"] += 1
        except Exception as exc:
            current_app.logger.warning(
                "bulk_issue: enrol failed for trainee %s: %s", row.trainee.id, exc
            )
            counts["errors"] += 1
    db.session.flush()
    return counts


# ─── Reminder dispatch ──────────────────────────────────────────────


REMINDER_KIND_3D = "3d"
REMINDER_KIND_7D = "7d"
REMINDER_KIND_FINAL = "final"


def _reminder_body(
    *, kind: str, lang: str, course_title: str, link: str, expires_at: datetime
) -> tuple[str, str]:
    """Return (subject, body_text) for one of the three reminder kinds.

    Bilingual (PL / EN); falls back to English for any other locale.
    Final-reminder copy is deliberately stronger — it tells the worker
    that today's the cutoff and they won't be cleared for work
    without finishing.
    """
    days_left = max(0, (expires_at.date() - _now().date()).days)
    if lang == "pl":
        if kind == REMINDER_KIND_FINAL:
            subject = f"[QMS] DZIŚ wygasa szkolenie: {course_title}"
            body = (
                f"DZIŚ MIJA TERMIN szkolenia „{course_title}”.\n"
                f"Bez zaliczenia szkolenia w dniu dzisiejszym nie zostaniesz "
                f"dopuszczony/-a do pracy.\n\n"
                f"Otwórz link i ukończ szkolenie:\n{link}\n"
            )
        elif kind == REMINDER_KIND_7D:
            subject = f"[QMS] Przypomnienie: {course_title}"
            body = (
                f"Zostało {days_left} dni na ukończenie szkolenia "
                f"„{course_title}”.\n\nLink:\n{link}\n"
            )
        else:  # 3d
            subject = f"[QMS] Twoje szkolenie czeka: {course_title}"
            body = (
                f"Trzy dni temu otrzymałeś/-aś link do szkolenia "
                f"„{course_title}”. Zostało {days_left} dni na "
                f"ukończenie.\n\nLink:\n{link}\n"
            )
    else:
        if kind == REMINDER_KIND_FINAL:
            subject = f"[QMS] FINAL — training expires today: {course_title}"
            body = (
                f"YOUR DEADLINE IS TODAY for the {course_title!r} training.\n"
                f"Without passing today, you will NOT be cleared for work.\n\n"
                f"Open the link and finish:\n{link}\n"
            )
        elif kind == REMINDER_KIND_7D:
            subject = f"[QMS] Reminder: {course_title}"
            body = (
                f"You have {days_left} day(s) left to finish the "
                f"{course_title!r} training.\n\nLink:\n{link}\n"
            )
        else:
            subject = f"[QMS] Your training is waiting: {course_title}"
            body = (
                f"Three days ago you received a link for the "
                f"{course_title!r} training. You have {days_left} day(s) "
                f"left to finish.\n\nLink:\n{link}\n"
            )
    return subject, body


def _classify_reminder(
    enrolment: TrainingEnrolment, *, now: datetime
) -> str | None:
    """Decide which reminder kind (if any) is due for `enrolment`.

    Rules:
    - Only ISSUED / STARTED enrolments. SUBMITTED is closed.
    - Token must still be live (`expires_at > now`).
    - `final` fires on the day `expires_at.date() == now.date()` AND
      hasn't been sent yet. Wins over the others — short-TTL collapse.
    - `7d` fires when `now >= issued_at + 7d` AND not yet sent AND
      we're NOT in the final-day window (otherwise final wins).
    - `3d` fires when `now >= issued_at + 3d` AND not yet sent.
    """
    if enrolment.status not in (
        EnrolmentStatus.ISSUED.value,
        EnrolmentStatus.STARTED.value,
    ):
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    expires = enrolment.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    issued = enrolment.issued_at
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=UTC)

    # The "final" reminder fires by calendar day, not by exact moment.
    # On the day the link expires we send the strong "today's the
    # deadline, you won't be cleared" copy regardless of whether the
    # link is technically still valid for a few more hours or has
    # just clicked over to expired this same day.
    is_final_day = expires.date() == now.date()
    if is_final_day and enrolment.reminder_final_sent_at is None:
        return REMINDER_KIND_FINAL

    # 3d / 7d only make sense while the link is still usable; once
    # expired, hand off to the EXPIRED sweep elsewhere.
    if expires <= now:
        return None

    if (
        not is_final_day
        and now >= issued + timedelta(days=7)
        and enrolment.reminder_7d_sent_at is None
    ):
        return REMINDER_KIND_7D
    if (
        now >= issued + timedelta(days=3)
        and enrolment.reminder_3d_sent_at is None
    ):
        return REMINDER_KIND_3D
    return None


def _mark_reminder_sent(
    enrolment: TrainingEnrolment, kind: str, *, now: datetime
) -> None:
    if kind == REMINDER_KIND_3D:
        enrolment.reminder_3d_sent_at = now
    elif kind == REMINDER_KIND_7D:
        enrolment.reminder_7d_sent_at = now
    elif kind == REMINDER_KIND_FINAL:
        enrolment.reminder_final_sent_at = now


def send_pending_reminders(*, base_url: str | None = None) -> dict[str, int]:
    """Walk every open enrolment, decide if a reminder is due, and
    dispatch it via the trainee's notification_channel.

    Returns counts dict for the scheduler log: `{"3d", "7d",
    "final", "skipped"}`. Caller commits.

    Idempotency: each send updates the matching `reminder_*_sent_at`
    column so the next pass treats this kind as done. The
    `_classify_reminder` predicate is the single decision gate; both
    new-enrolments and ones we'd already touched go through it.
    """
    now = _now()
    counts = {"3d": 0, "7d": 0, "final": 0, "skipped": 0}
    open_states = (EnrolmentStatus.ISSUED.value, EnrolmentStatus.STARTED.value)
    enrolments = (
        TrainingEnrolment.query
        .filter(TrainingEnrolment.status.in_(open_states))
        .filter(TrainingEnrolment.expires_at > now)
        .all()
    )
    for enrolment in enrolments:
        kind = _classify_reminder(enrolment, now=now)
        if kind is None:
            continue
        trainee = enrolment.trainee
        if trainee is None or not trainee.is_active:
            counts["skipped"] += 1
            continue
        course = enrolment.course_version.course
        version = enrolment.course_version
        title = (
            (version.title or {}).get(trainee.language or "en")
            or (version.title or {}).get("en")
            or course.code
        )
        base = base_url or current_app.config.get("TRAINING_BASE_URL", "")
        link = f"{base.rstrip('/')}/training/take/{enrolment.magic_token}"
        subject, body = _reminder_body(
            kind=kind, lang=trainee.language or "en",
            course_title=title, link=link, expires_at=enrolment.expires_at,
        )

        # Use the same channel-aware dispatch as the original enrol —
        # SMS / email / both per trainee preference, with the same
        # email-required-or-fallback semantics. We don't go through
        # `_deliver_magic_link` because the body / subject differ;
        # but we mirror its try/except + logger.warning best-effort
        # pattern so a Redis / SMTP outage doesn't 500 the loop.
        channel = trainee.notification_channel
        has_email = bool((trainee.email or "").strip())
        want_sms = channel in (
            NotificationChannel.SMS.value, NotificationChannel.BOTH.value
        )
        want_email = channel in (
            NotificationChannel.EMAIL.value, NotificationChannel.BOTH.value
        )
        if want_email and not has_email:
            current_app.logger.warning(
                "training-reminder %s: trainee %s has no email; falling back to SMS",
                kind, trainee.id,
            )
            want_email = False
            want_sms = True

        if want_sms:
            try:
                # SMS body trimmed to the first 160ish chars by ClickSend
                # automatically; we just send the same text.
                enqueue_sms(to=trainee.phone, body=body)
            except Exception as exc:
                current_app.logger.warning(
                    "training-reminder %s: SMS enqueue failed for %s: %s",
                    kind, trainee.id, exc,
                )
        if want_email:
            try:
                enqueue_email(
                    to=trainee.email, subject=subject,
                    body_text=body, body_html=None,
                )
            except Exception as exc:
                current_app.logger.warning(
                    "training-reminder %s: email enqueue failed for %s: %s",
                    kind, trainee.id, exc,
                )

        _mark_reminder_sent(enrolment, kind, now=now)
        audit.record(
            entity_type="training_enrolment",
            entity_id=enrolment.id,
            action=AuditAction.TRAINING_REMINDER_SENT,
            diff={
                "kind": kind,
                "channel_requested": channel,
                "phone_suffix": trainee.phone[-4:],
            },
        )
        counts[kind] += 1

    db.session.flush()
    return counts
