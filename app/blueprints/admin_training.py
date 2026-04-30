"""Admin pages for training: courses, trainees, dashboard.

Mounted under /admin/training/* and registered alongside admin_bp in
app/__init__.py. Permission gating uses TRAINING_AUTHOR (course
authoring), TRAINING_SEND (trainee CRUD + ad-hoc enrolment),
TRAINING_REVIEW (read-only dashboard + attempt detail).
"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required
from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import (
    BooleanField,
    DecimalField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.audit_actions import AuditAction
from app.auth import require_permission
from app.constants import (
    MAX_CODE_LENGTH,
    MAX_FULL_NAME_LENGTH,
    MAX_NAME_BILINGUAL_LENGTH,
)
from app.extensions import db
from app.i18n import gettext as _
from app.i18n import language_choices
from app.models import (
    EnrolmentStatus,
    ProductionLine,
    TrainingAssignment,
    TrainingAttempt,
    TrainingCertification,
    TrainingCourse,
    TrainingCourseVersion,
    TrainingEnrolment,
    Trainee,
)
from app.permissions import Perm
from app.services import audit
from app.services import training as training_service

bp = Blueprint("admin_training", __name__, url_prefix="/admin/training")


# ─── Forms ──────────────────────────────────────────────────────────


class TraineeForm(FlaskForm):
    phone = StringField("phone", validators=[DataRequired(), Length(max=32)])
    full_name = StringField(
        "full_name", validators=[DataRequired(), Length(max=MAX_FULL_NAME_LENGTH)]
    )
    role_code = StringField(
        "role_code", validators=[DataRequired(), Length(max=32)]
    )
    line_code = SelectField("line_code", validators=[Optional()])
    language = SelectField("language")
    is_active = BooleanField("is_active", default=True)
    submit = SubmitField()


class CourseForm(FlaskForm):
    code = StringField("code", validators=[DataRequired(), Length(max=MAX_CODE_LENGTH)])
    description = TextAreaField("description", validators=[Optional(), Length(max=2000)])
    submit = SubmitField()


class CourseVersionForm(FlaskForm):
    title_pl = StringField(
        "title_pl",
        validators=[DataRequired(), Length(max=MAX_NAME_BILINGUAL_LENGTH)],
    )
    title_en = StringField(
        "title_en",
        validators=[DataRequired(), Length(max=MAX_NAME_BILINGUAL_LENGTH)],
    )
    summary_pl = TextAreaField(
        "summary_pl", validators=[Optional(), Length(max=2000)]
    )
    summary_en = TextAreaField(
        "summary_en", validators=[Optional(), Length(max=2000)]
    )
    pass_threshold = DecimalField(
        "pass_threshold",
        validators=[DataRequired(), NumberRange(min=0.0, max=1.0)],
        default=0.7,
        places=2,
    )
    validity_months = IntegerField(
        "validity_months",
        validators=[DataRequired(), NumberRange(min=1, max=120)],
        default=12,
    )
    link_ttl_days = IntegerField(
        "link_ttl_days",
        validators=[DataRequired(), NumberRange(min=1, max=90)],
        default=7,
    )
    submit = SubmitField()


class AssignmentForm(FlaskForm):
    role_code = StringField(
        "role_code", validators=[Optional(), Length(max=32)]
    )
    line_code = SelectField("line_code", validators=[Optional()])
    recurrence_months = IntegerField(
        "recurrence_months",
        validators=[DataRequired(), NumberRange(min=1, max=120)],
        default=12,
    )
    submit = SubmitField()


class IssueLinkForm(FlaskForm):
    course_code = SelectField("course_code", validators=[DataRequired()])
    submit = SubmitField()


# ─── Helpers ────────────────────────────────────────────────────────


def _line_choices() -> list[tuple[str, str]]:
    rows = ProductionLine.query.filter_by(is_active=True).order_by(ProductionLine.code).all()
    return [("", "—")] + [(r.code, r.code) for r in rows]


def _course_choices() -> list[tuple[str, str]]:
    rows = TrainingCourse.query.filter_by(is_active=True).order_by(TrainingCourse.code).all()
    return [(r.code, r.code) for r in rows]


def _resolve_line_id(code: str | None) -> str | None:
    if not code:
        return None
    line = ProductionLine.query.filter_by(code=code).first()
    return line.id if line else None


# ─── Trainees ───────────────────────────────────────────────────────


@bp.route("/trainees")
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainees_index():
    rows = Trainee.query.order_by(Trainee.full_name).all()
    return render_template("admin/training/trainees_list.html", trainees=rows)


@bp.route("/trainees/new", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainees_new():
    form = TraineeForm()
    form.line_code.choices = _line_choices()
    form.language.choices = _language_choices()
    if form.validate_on_submit():
        try:
            trainee = training_service.create_trainee(
                phone=form.phone.data.strip(),
                full_name=form.full_name.data.strip(),
                role_code=form.role_code.data.strip(),
                line_id=_resolve_line_id(form.line_code.data),
                language=form.language.data,
            )
            db.session.commit()
            flash(_("admin.training.trainee.created"), "success")
            return redirect(url_for("admin_training.trainees_index"))
        except training_service.TrainingError as exc:
            flash(str(exc), "danger")
    return render_template(
        "admin/training/trainee_form.html", form=form, edit=False
    )


def _language_choices() -> list[tuple[str, str]]:
    from flask import current_app

    return language_choices(tuple(current_app.config["SUPPORTED_LANGUAGES"]))


@bp.route("/trainees/<trainee_id>", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainees_edit(trainee_id: str):
    trainee = db.session.get(Trainee, trainee_id)
    if trainee is None:
        abort(404)
    form = TraineeForm()
    form.line_code.choices = _line_choices()
    form.language.choices = _language_choices()
    if request.method == "GET":
        form.phone.data = trainee.phone
        form.full_name.data = trainee.full_name
        form.role_code.data = trainee.role_code
        form.is_active.data = trainee.is_active
        form.language.data = trainee.language
        if trainee.line_id:
            line = db.session.get(ProductionLine, trainee.line_id)
            if line:
                form.line_code.data = line.code
    if form.validate_on_submit():
        prev = {
            "full_name": trainee.full_name,
            "role_code": trainee.role_code,
            "is_active": trainee.is_active,
        }
        trainee.full_name = form.full_name.data.strip()
        trainee.role_code = form.role_code.data.strip()
        trainee.language = form.language.data
        trainee.is_active = form.is_active.data
        trainee.line_id = _resolve_line_id(form.line_code.data)
        audit.record(
            entity_type="trainee",
            entity_id=trainee.id,
            action=AuditAction.UPDATE,
            diff={"before": prev},
        )
        db.session.commit()
        flash(_("admin.training.trainee.updated"), "success")
        return redirect(url_for("admin_training.trainees_index"))
    return render_template(
        "admin/training/trainee_form.html", form=form, edit=True, trainee=trainee
    )


@bp.route("/trainees/<trainee_id>/issue", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainees_issue(trainee_id: str):
    """Manually enrol a trainee in a course (admin override)."""
    trainee = db.session.get(Trainee, trainee_id)
    if trainee is None:
        abort(404)
    form = IssueLinkForm()
    form.course_code.choices = _course_choices()
    if form.validate_on_submit():
        course = training_service.get_course_by_code(form.course_code.data)
        if course is None:
            flash(_("admin.training.course.not_found"), "danger")
        else:
            try:
                training_service.enrol(trainee=trainee, course=course)
                db.session.commit()
                flash(_("admin.training.link_issued"), "success")
                return redirect(url_for("admin_training.trainees_index"))
            except training_service.TrainingError as exc:
                flash(str(exc), "danger")
    return render_template(
        "admin/training/trainee_issue.html", form=form, trainee=trainee
    )


# ─── Courses ────────────────────────────────────────────────────────


@bp.route("/courses")
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def courses_index():
    rows = TrainingCourse.query.order_by(TrainingCourse.code).all()
    return render_template("admin/training/courses_list.html", courses=rows)


@bp.route("/courses/new", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def courses_new():
    form = CourseForm()
    if form.validate_on_submit():
        try:
            course = training_service.create_course(
                code=form.code.data.strip(),
                description=(form.description.data or "").strip() or None,
            )
            db.session.commit()
            flash(_("admin.training.course.created"), "success")
            return redirect(url_for("admin_training.courses_edit", course_id=course.id))
        except training_service.TrainingError as exc:
            flash(str(exc), "danger")
    return render_template("admin/training/course_form.html", form=form, edit=False)


@bp.route("/courses/<course_id>", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def courses_edit(course_id: str):
    """Add a new immutable version of a course. Older versions stay
    around so in-flight enrolments keep working."""
    course = db.session.get(TrainingCourse, course_id)
    if course is None:
        abort(404)
    form = CourseVersionForm()
    if request.method == "GET":
        active = course.active_version
        if active is not None:
            form.title_pl.data = (active.title or {}).get("pl", "")
            form.title_en.data = (active.title or {}).get("en", "")
            form.summary_pl.data = (active.summary or {}).get("pl", "")
            form.summary_en.data = (active.summary or {}).get("en", "")
            form.pass_threshold.data = active.pass_threshold
            form.validity_months.data = active.validity_months
            form.link_ttl_days.data = active.link_ttl_days
    if form.validate_on_submit():
        version = training_service.add_course_version(
            course=course,
            title={"pl": form.title_pl.data.strip(), "en": form.title_en.data.strip()},
            summary={"pl": form.summary_pl.data or "", "en": form.summary_en.data or ""},
            pass_threshold=float(form.pass_threshold.data),
            validity_months=int(form.validity_months.data),
            link_ttl_days=int(form.link_ttl_days.data),
        )
        db.session.commit()
        flash(
            _("admin.training.course.versioned").format(version=version.version),
            "success",
        )
        return redirect(url_for("admin_training.courses_edit", course_id=course.id))
    versions = sorted(course.versions, key=lambda v: v.version, reverse=True)
    return render_template(
        "admin/training/course_form.html",
        form=form,
        course=course,
        versions=versions,
        edit=True,
    )


# ─── Dashboard ──────────────────────────────────────────────────────


@bp.route("/dashboard")
@login_required
@require_permission(Perm.TRAINING_REVIEW)
def dashboard():
    """Per-trainee × per-course matrix with cert state."""
    trainees = Trainee.query.filter_by(is_active=True).order_by(Trainee.full_name).all()
    courses = TrainingCourse.query.filter_by(is_active=True).order_by(TrainingCourse.code).all()
    rows: list[dict] = []
    for trainee in trainees:
        cells = []
        for course in courses:
            cert = training_service.latest_certification(trainee.id, course.id)
            due = training_service.is_due_for_recert(trainee, course)
            cells.append({"course": course, "cert": cert, "due": due})
        rows.append({"trainee": trainee, "cells": cells})
    open_enrolments = (
        db.session.execute(
            select(TrainingEnrolment)
            .where(TrainingEnrolment.status.in_(
                (EnrolmentStatus.ISSUED.value, EnrolmentStatus.STARTED.value)
            ))
            .order_by(TrainingEnrolment.issued_at.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    return render_template(
        "admin/training/dashboard.html",
        rows=rows,
        courses=courses,
        open_enrolments=open_enrolments,
    )
