"""CSV export / import helpers for trainees + courses.

Exports are deterministic, UTF-8 with BOM (Excel-friendly), comma
separated. Templates are exports without data — header row + one
example row commented in.

Imports are two-pass: first the CSV is parsed and validated row-by-row
into a `ImportPlan` (no DB writes), then `apply_plan()` commits all
rows in one transaction. The view layer can render the plan as a
preview before calling apply.

`employee_number` (trainees) and `code` (courses) are the stable
identifiers used for upsert lookup. Phone / email / line move
freely; identity stays.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Iterable

from app.extensions import db
from app.models import (
    NotificationChannel,
    ProductionLine,
    Trainee,
    TrainingCourse,
)
from app.services import training as training_service

# ─── shared CSV helpers ────────────────────────────────────────────


_BOM = "﻿"


def _serialise(rows: Iterable[list[str]]) -> bytes:
    buf = io.StringIO()
    buf.write(_BOM)
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _parse(blob: bytes) -> list[dict[str, str]]:
    """Return a list of {column: value} dicts. Strips BOM if present.
    Empty / blank lines are dropped."""
    text = blob.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict[str, str]] = []
    for row in reader:
        if not any((v or "").strip() for v in row.values()):
            continue
        out.append({k: (v or "").strip() for k, v in row.items()})
    return out


# ─── trainees CSV ──────────────────────────────────────────────────


TRAINEE_COLUMNS: tuple[str, ...] = (
    "employee_number",
    "phone",
    "email",
    "full_name",
    "role_code",
    "line_code",
    "language",
    "notification_channel",
    "is_active",
)


def export_trainees() -> bytes:
    """All active + inactive trainees, ordered by employee_number then
    full_name so the CSV diff stays stable across exports."""
    rows: list[list[str]] = [list(TRAINEE_COLUMNS)]
    line_by_id = {ln.id: ln.code for ln in ProductionLine.query.all()}
    trainees = (
        Trainee.query.order_by(
            Trainee.employee_number.asc().nulls_last(),
            Trainee.full_name.asc(),
        )
        .all()
    )
    for t in trainees:
        rows.append([
            t.employee_number or "",
            t.phone,
            t.email or "",
            t.full_name,
            t.role_code,
            line_by_id.get(t.line_id, "") if t.line_id else "",
            t.language,
            t.notification_channel,
            "true" if t.is_active else "false",
        ])
    return _serialise(rows)


def trainees_template() -> bytes:
    """Header + one annotated example row (operator can replace and
    re-upload)."""
    return _serialise([
        list(TRAINEE_COLUMNS),
        [
            "EMP-001",
            "+447700000001",
            "anna@example.com",
            "Anna Kowalska",
            "operator",
            "",  # line_code (blank = no line)
            "pl",
            "sms",
            "true",
        ],
    ])


# ─── trainees import plan ──────────────────────────────────────────


@dataclass
class TraineeRowResult:
    """One CSV row + the action we'd take + any per-row errors."""

    line_no: int
    raw: dict[str, str]
    action: str = "skip"  # "create" | "update" | "skip"
    errors: list[str] = field(default_factory=list)
    matched_trainee_id: str | None = None
    diff: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass
class TraineeImportPlan:
    rows: list[TraineeRowResult] = field(default_factory=list)

    @property
    def n_create(self) -> int:
        return sum(1 for r in self.rows if r.action == "create" and not r.errors)

    @property
    def n_update(self) -> int:
        return sum(1 for r in self.rows if r.action == "update" and not r.errors)

    @property
    def n_error(self) -> int:
        return sum(1 for r in self.rows if r.errors)


def plan_trainees_import(blob: bytes) -> TraineeImportPlan:
    """Parse + validate without touching the DB."""
    plan = TraineeImportPlan()
    try:
        rows = _parse(blob)
    except UnicodeDecodeError:
        plan.rows.append(
            TraineeRowResult(
                line_no=0, raw={}, action="skip",
                errors=["file is not UTF-8 — re-export or save as UTF-8 CSV"],
            )
        )
        return plan

    line_codes_seen = set()
    line_by_code = {ln.code: ln.id for ln in ProductionLine.query.all()}
    valid_channels = {c.value for c in NotificationChannel}

    # Track in-CSV duplicates: same employee_number / phone in two rows
    # would silently apply twice.
    seen_emp: dict[str, int] = {}
    seen_phone: dict[str, int] = {}

    for idx, raw in enumerate(rows, start=2):  # 1 = header
        result = TraineeRowResult(line_no=idx, raw=raw)
        emp = raw.get("employee_number", "").strip()
        phone = raw.get("phone", "").strip()
        email = raw.get("email", "").strip() or None
        full_name = raw.get("full_name", "").strip()
        role_code = raw.get("role_code", "").strip()
        line_code = raw.get("line_code", "").strip()
        language = (raw.get("language", "") or "en").strip() or "en"
        channel = (raw.get("notification_channel", "") or "sms").strip() or "sms"
        is_active_raw = (raw.get("is_active", "") or "true").strip().lower()
        is_active = is_active_raw in ("true", "1", "yes", "y", "tak", "t")

        # Required fields.
        if not emp:
            result.errors.append("employee_number is required")
        if not phone:
            result.errors.append("phone is required")
        if not full_name:
            result.errors.append("full_name is required")
        if not role_code:
            result.errors.append("role_code is required")
        if channel not in valid_channels:
            result.errors.append(f"unknown notification_channel: {channel!r}")
        if line_code and line_code not in line_by_code:
            result.errors.append(f"unknown line_code: {line_code!r}")
        if line_code:
            line_codes_seen.add(line_code)
        if (channel != "sms") and not email:
            result.errors.append(
                "email is required for notification_channel=email/both"
            )

        # In-CSV duplicates.
        if emp:
            if emp in seen_emp:
                result.errors.append(
                    f"duplicate employee_number in this CSV (also row {seen_emp[emp]})"
                )
            else:
                seen_emp[emp] = idx
        if phone:
            if phone in seen_phone:
                result.errors.append(
                    f"duplicate phone in this CSV (also row {seen_phone[phone]})"
                )
            else:
                seen_phone[phone] = idx

        if result.errors:
            plan.rows.append(result)
            continue

        # Lookup existing.
        existing = Trainee.query.filter_by(employee_number=emp).first()
        if existing is None:
            # Phone may belong to someone else who hasn't got an emp_no
            # yet — that would create a duplicate phone, which the
            # service rejects. Surface here for a useful message.
            other = Trainee.query.filter_by(phone=phone).first()
            if other is not None:
                result.errors.append(
                    f"phone {phone} already belongs to existing trainee "
                    f"(employee_number={other.employee_number or '—'}); "
                    f"give them this employee_number first"
                )
                plan.rows.append(result)
                continue
            result.action = "create"
        else:
            # Phone collision against a *different* trainee.
            other = (
                Trainee.query.filter(Trainee.phone == phone)
                .filter(Trainee.id != existing.id).first()
            )
            if other is not None:
                result.errors.append(
                    f"phone {phone} already belongs to trainee "
                    f"{other.employee_number or other.id} — refusing to swap silently"
                )
                plan.rows.append(result)
                continue
            result.action = "update"
            result.matched_trainee_id = existing.id
            # Compute diff so the preview can show what changes.
            for col, new in (
                ("phone", phone),
                ("email", email or ""),
                ("full_name", full_name),
                ("role_code", role_code),
                ("line_code", line_code),
                ("language", language),
                ("notification_channel", channel),
                ("is_active", str(is_active).lower()),
            ):
                if col == "line_code":
                    cur = ""
                    if existing.line_id:
                        cur_line = ProductionLine.query.get(existing.line_id)
                        cur = cur_line.code if cur_line else ""
                else:
                    cur_attr = getattr(existing, col)
                    cur = "" if cur_attr is None else str(cur_attr).lower() if col == "is_active" else str(cur_attr)
                if cur != new:
                    result.diff[col] = (cur, new)

        plan.rows.append(result)

    return plan


def apply_trainees_plan(plan: TraineeImportPlan) -> dict[str, int]:
    """Commit every error-free row. Returns a counts dict.
    Caller is responsible for the surrounding flask request /
    db.session.commit() — we flush each change but commit only at the
    end so a late integrity error rolls everything back."""
    line_by_code = {ln.code: ln.id for ln in ProductionLine.query.all()}
    counts = {"created": 0, "updated": 0, "skipped": 0}
    for r in plan.rows:
        if r.errors:
            counts["skipped"] += 1
            continue
        raw = r.raw
        line_id = line_by_code.get(raw.get("line_code", "").strip()) or None
        is_active_raw = (raw.get("is_active", "") or "true").strip().lower()
        is_active = is_active_raw in ("true", "1", "yes", "y", "tak", "t")
        email = raw.get("email", "").strip() or None
        if r.action == "create":
            training_service.create_trainee(
                employee_number=raw["employee_number"].strip(),
                phone=raw["phone"].strip(),
                email=email,
                full_name=raw["full_name"].strip(),
                role_code=raw["role_code"].strip(),
                line_id=line_id,
                language=(raw.get("language", "") or "en").strip(),
                notification_channel=(raw.get("notification_channel", "") or "sms").strip(),
            )
            counts["created"] += 1
        else:  # update
            tr = db.session.get(Trainee, r.matched_trainee_id)
            if tr is None:
                counts["skipped"] += 1
                continue
            tr.phone = raw["phone"].strip()
            tr.email = email
            tr.full_name = raw["full_name"].strip()
            tr.role_code = raw["role_code"].strip()
            tr.line_id = line_id
            tr.language = (raw.get("language", "") or "en").strip()
            tr.notification_channel = (raw.get("notification_channel", "") or "sms").strip()
            tr.is_active = is_active
            counts["updated"] += 1
    db.session.flush()
    return counts


# ─── courses CSV ───────────────────────────────────────────────────


COURSE_COLUMNS: tuple[str, ...] = (
    "code",
    "is_active",
    "description",
    "title_pl",
    "title_en",
    "summary_pl",
    "summary_en",
    "pass_threshold",
    "validity_months",
    "link_ttl_days",
)


def export_courses() -> bytes:
    """All courses with their *active* version's content. One row per
    course; if there's no active version, the version-fields are blank."""
    rows: list[list[str]] = [list(COURSE_COLUMNS)]
    courses = TrainingCourse.query.order_by(TrainingCourse.code).all()
    for c in courses:
        v = c.active_version
        title_pl = (v.title or {}).get("pl", "") if v else ""
        title_en = (v.title or {}).get("en", "") if v else ""
        summary_pl = (v.summary or {}).get("pl", "") if v else ""
        summary_en = (v.summary or {}).get("en", "") if v else ""
        pass_threshold = str(v.pass_threshold) if v else ""
        validity_months = str(v.validity_months) if v else ""
        link_ttl_days = str(v.link_ttl_days) if v else ""
        rows.append([
            c.code,
            "true" if c.is_active else "false",
            c.description or "",
            title_pl,
            title_en,
            summary_pl,
            summary_en,
            pass_threshold,
            validity_months,
            link_ttl_days,
        ])
    return _serialise(rows)


def courses_template() -> bytes:
    return _serialise([
        list(COURSE_COLUMNS),
        [
            "HACCP-REFRESHER",
            "true",
            "Annual HACCP refresher training",
            "Odświeżenie HACCP",
            "HACCP refresher",
            "Roczne odświeżenie zasad HACCP.",
            "Annual review of HACCP principles.",
            "0.7",
            "12",
            "7",
        ],
    ])


# ─── courses import plan ───────────────────────────────────────────


@dataclass
class CourseRowResult:
    line_no: int
    raw: dict[str, str]
    action: str = "skip"
    errors: list[str] = field(default_factory=list)
    matched_course_id: str | None = None


@dataclass
class CourseImportPlan:
    rows: list[CourseRowResult] = field(default_factory=list)

    @property
    def n_create(self) -> int:
        return sum(1 for r in self.rows if r.action == "create" and not r.errors)

    @property
    def n_update(self) -> int:
        return sum(1 for r in self.rows if r.action == "update" and not r.errors)

    @property
    def n_error(self) -> int:
        return sum(1 for r in self.rows if r.errors)


def plan_courses_import(blob: bytes) -> CourseImportPlan:
    plan = CourseImportPlan()
    try:
        rows = _parse(blob)
    except UnicodeDecodeError:
        plan.rows.append(
            CourseRowResult(
                line_no=0, raw={}, action="skip",
                errors=["file is not UTF-8"],
            )
        )
        return plan

    seen_codes: dict[str, int] = {}
    for idx, raw in enumerate(rows, start=2):
        result = CourseRowResult(line_no=idx, raw=raw)
        code = raw.get("code", "").strip()
        if not code:
            result.errors.append("code is required")
        if not raw.get("title_pl", "").strip():
            result.errors.append("title_pl is required")
        if not raw.get("title_en", "").strip():
            result.errors.append("title_en is required")
        try:
            pt = float(raw.get("pass_threshold", "") or 0.7)
            if not 0 <= pt <= 1:
                result.errors.append("pass_threshold must be 0..1")
        except ValueError:
            result.errors.append("pass_threshold is not a number")
        try:
            vm = int(raw.get("validity_months", "") or 12)
            if not 1 <= vm <= 120:
                result.errors.append("validity_months must be 1..120")
        except ValueError:
            result.errors.append("validity_months is not an integer")
        try:
            lt = int(raw.get("link_ttl_days", "") or 7)
            if not 1 <= lt <= 90:
                result.errors.append("link_ttl_days must be 1..90")
        except ValueError:
            result.errors.append("link_ttl_days is not an integer")

        if code:
            if code in seen_codes:
                result.errors.append(
                    f"duplicate code in this CSV (also row {seen_codes[code]})"
                )
            else:
                seen_codes[code] = idx

        if result.errors:
            plan.rows.append(result)
            continue

        existing = TrainingCourse.query.filter_by(code=code).first()
        if existing is None:
            result.action = "create"
        else:
            result.action = "update"
            result.matched_course_id = existing.id
        plan.rows.append(result)

    return plan


def apply_courses_plan(plan: CourseImportPlan) -> dict[str, int]:
    counts = {"created": 0, "updated": 0, "skipped": 0}
    for r in plan.rows:
        if r.errors:
            counts["skipped"] += 1
            continue
        raw = r.raw
        code = raw["code"].strip()
        title = {
            "pl": raw["title_pl"].strip(),
            "en": raw["title_en"].strip(),
        }
        summary = {
            "pl": (raw.get("summary_pl", "") or "").strip(),
            "en": (raw.get("summary_en", "") or "").strip(),
        }
        pass_threshold = float(raw.get("pass_threshold", "") or 0.7)
        validity_months = int(raw.get("validity_months", "") or 12)
        link_ttl_days = int(raw.get("link_ttl_days", "") or 7)
        is_active_raw = (raw.get("is_active", "") or "true").strip().lower()
        is_active = is_active_raw in ("true", "1", "yes", "y", "tak", "t")
        description = (raw.get("description", "") or "").strip() or None

        if r.action == "create":
            course = training_service.create_course(code=code, description=description)
            training_service.add_course_version(
                course=course,
                title=title,
                summary=summary,
                pass_threshold=pass_threshold,
                validity_months=validity_months,
                link_ttl_days=link_ttl_days,
            )
            course.is_active = is_active
            counts["created"] += 1
        else:
            course = db.session.get(TrainingCourse, r.matched_course_id)
            if course is None:
                counts["skipped"] += 1
                continue
            course.description = description
            course.is_active = is_active
            # Always create a fresh version on update — same pattern
            # as the admin "Save as new version" flow.
            training_service.add_course_version(
                course=course,
                title=title,
                summary=summary,
                pass_threshold=pass_threshold,
                validity_months=validity_months,
                link_ttl_days=link_ttl_days,
            )
            counts["updated"] += 1
    db.session.flush()
    return counts
