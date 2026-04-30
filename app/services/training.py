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

from datetime import datetime, timedelta, timezone
from typing import Iterable

from flask import current_app
from sqlalchemy import select

from app.audit_actions import AuditAction
from app.extensions import db
from app.models import (
    EnrolmentSource,
    EnrolmentStatus,
    Trainee,
    TrainingAnswerOption,
    TrainingAttempt,
    TrainingCertification,
    TrainingCourse,
    TrainingCourseVersion,
    TrainingDeclaration,
    TrainingEnrolment,
    TrainingQuestion,
)
from app.services import audit
from app.services import training_links
from app.services.queue import enqueue_sms


class TrainingError(Exception):
    """Domain error from the training service."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_past(dt: datetime) -> bool:
    """Compare a possibly-naive `dt` (e.g. from SQLite read-back, which
    drops tzinfo) against now. Treats naive as UTC. PG preserves
    tzinfo correctly; this is a portability shim."""
    if dt.tzinfo is None:
        return dt.timestamp() <= datetime.now().timestamp()
    return dt <= _now()


# ─── Trainee + course CRUD ─────────────────────────────────────────


def create_trainee(
    *,
    phone: str,
    full_name: str,
    role_code: str,
    line_id: str | None = None,
    language: str = "en",
) -> Trainee:
    if not phone or not full_name or not role_code:
        raise TrainingError("phone, full_name, role_code are required")
    if Trainee.query.filter_by(phone=phone).first():
        raise TrainingError(f"phone {phone} already registered")
    trainee = Trainee(
        phone=phone,
        full_name=full_name,
        role_code=role_code,
        line_id=line_id,
        language=language,
    )
    db.session.add(trainee)
    db.session.flush()
    audit.record(
        entity_type="trainee",
        entity_id=trainee.id,
        action=AuditAction.CREATE,
        diff={"phone": phone, "role_code": role_code},
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
    enqueue_sms(to=trainee.phone, body=body)
    audit.record(
        entity_type="training_enrolment",
        entity_id=enrolment.id,
        action=AuditAction.TRAINING_LINK_SENT,
        diff={"phone_suffix": trainee.phone[-4:]},
    )
    return enrolment


def get_enrolment_by_token(token: str) -> TrainingEnrolment | None:
    """Verify the token and return the enrolment if it's still live.

    Returns None when the token is malformed/expired/forged or when the
    stored expires_at is past — same negative result for all three so we
    don't leak which case applied.
    """
    try:
        enrolment_id = training_links.verify_token(token)
    except training_links.InvalidToken:
        return None
    enrolment = db.session.get(TrainingEnrolment, enrolment_id)
    if enrolment is None:
        return None
    if enrolment.magic_token != token:
        return None
    if _is_past(enrolment.expires_at):
        return None
    return enrolment


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
        # 30-day months are good enough for cert lifetime; precision isn't material.
        valid_until = valid_from + timedelta(days=30 * version.validity_months)
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

    Due if: there is no live cert (never trained, or cert expired) AND no
    open enrolment in flight.
    """
    if has_open_enrolment(trainee.id, course.id):
        return False
    cert = latest_certification(trainee.id, course.id)
    if cert is None:
        return True
    return _is_past(cert.valid_until)


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
            if a.role_code is not None and a.role_code != trainee.role_code:
                continue
            if a.line_id is not None and a.line_id != trainee.line_id:
                continue
            if trainee.id in seen:
                continue
            if is_due_for_recert(trainee, course):
                out.append(trainee)
                seen.add(trainee.id)
            break
    return out
