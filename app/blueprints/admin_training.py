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
    QuestionKind,
    TrainingAnswerOption,
    TrainingAssignment,
    TrainingAttempt,
    TrainingCertification,
    TrainingCourse,
    TrainingCourseVersion,
    TrainingEnrolment,
    TrainingModule,
    TrainingQuestion,
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


class ModuleForm(FlaskForm):
    title_pl = StringField(
        "title_pl",
        validators=[DataRequired(), Length(max=MAX_NAME_BILINGUAL_LENGTH)],
    )
    title_en = StringField(
        "title_en",
        validators=[DataRequired(), Length(max=MAX_NAME_BILINGUAL_LENGTH)],
    )
    body_pl = TextAreaField("body_pl", validators=[DataRequired(), Length(max=20000)])
    body_en = TextAreaField("body_en", validators=[DataRequired(), Length(max=20000)])
    submit = SubmitField()


class QuestionForm(FlaskForm):
    """4 fixed answer-option slots — empty rows are skipped on save.
    Covers single_choice / multi_choice / true_false (use 2 slots)."""

    prompt_pl = TextAreaField(
        "prompt_pl",
        validators=[DataRequired(), Length(max=2000)],
    )
    prompt_en = TextAreaField(
        "prompt_en",
        validators=[DataRequired(), Length(max=2000)],
    )
    kind = SelectField(
        "kind",
        choices=[(k.value, k.value) for k in QuestionKind],
        default=QuestionKind.SINGLE_CHOICE.value,
    )
    opt1_pl = StringField("opt1_pl", validators=[Optional(), Length(max=500)])
    opt1_en = StringField("opt1_en", validators=[Optional(), Length(max=500)])
    opt1_correct = BooleanField("opt1_correct", default=False)
    opt2_pl = StringField("opt2_pl", validators=[Optional(), Length(max=500)])
    opt2_en = StringField("opt2_en", validators=[Optional(), Length(max=500)])
    opt2_correct = BooleanField("opt2_correct", default=False)
    opt3_pl = StringField("opt3_pl", validators=[Optional(), Length(max=500)])
    opt3_en = StringField("opt3_en", validators=[Optional(), Length(max=500)])
    opt3_correct = BooleanField("opt3_correct", default=False)
    opt4_pl = StringField("opt4_pl", validators=[Optional(), Length(max=500)])
    opt4_en = StringField("opt4_en", validators=[Optional(), Length(max=500)])
    opt4_correct = BooleanField("opt4_correct", default=False)
    submit = SubmitField()

    def options_payload(self) -> list[tuple[dict, bool]]:
        """Squash the 4 fixed slots down to a list of (label, is_correct).
        Skip slots with no PL or no EN text."""
        slots = [
            (self.opt1_pl.data, self.opt1_en.data, bool(self.opt1_correct.data)),
            (self.opt2_pl.data, self.opt2_en.data, bool(self.opt2_correct.data)),
            (self.opt3_pl.data, self.opt3_en.data, bool(self.opt3_correct.data)),
            (self.opt4_pl.data, self.opt4_en.data, bool(self.opt4_correct.data)),
        ]
        out: list[tuple[dict, bool]] = []
        for pl, en, correct in slots:
            pl = (pl or "").strip()
            en = (en or "").strip()
            if not pl and not en:
                continue
            out.append(({"pl": pl, "en": en}, correct))
        return out


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


# ─── Modules ────────────────────────────────────────────────────────


def _resolve_active_version(course_id: str) -> tuple[TrainingCourse, TrainingCourseVersion]:
    course = db.session.get(TrainingCourse, course_id)
    if course is None:
        abort(404)
    version = course.active_version
    if version is None:
        flash(_("admin.training.course.no_active_version"), "danger")
        abort(404)
    return course, version


@bp.route("/courses/<course_id>/modules")
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def modules_index(course_id: str):
    course, version = _resolve_active_version(course_id)
    return render_template(
        "admin/training/modules_list.html",
        course=course,
        version=version,
        modules=version.modules,
    )


@bp.route("/courses/<course_id>/modules/new", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def modules_new(course_id: str):
    course, version = _resolve_active_version(course_id)
    form = ModuleForm()
    if form.validate_on_submit():
        next_idx = 1 + max(
            (m.order_index for m in version.modules), default=-1
        )
        mod = TrainingModule(
            course_version_id=version.id,
            order_index=next_idx,
            title={"pl": form.title_pl.data.strip(), "en": form.title_en.data.strip()},
            body_md={"pl": form.body_pl.data, "en": form.body_en.data},
        )
        db.session.add(mod)
        audit.record(
            entity_type="training_module",
            entity_id=mod.id,
            action=AuditAction.CREATE,
            diff={"course_version_id": version.id, "order_index": next_idx},
        )
        db.session.commit()
        flash(_("admin.training.module.created"), "success")
        return redirect(url_for("admin_training.modules_index", course_id=course.id))
    return render_template(
        "admin/training/module_form.html",
        course=course,
        version=version,
        form=form,
        edit=False,
    )


@bp.route(
    "/courses/<course_id>/modules/<module_id>", methods=["GET", "POST"]
)
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def modules_edit(course_id: str, module_id: str):
    course, version = _resolve_active_version(course_id)
    mod = db.session.get(TrainingModule, module_id)
    if mod is None or mod.course_version_id != version.id:
        abort(404)
    form = ModuleForm()
    if request.method == "GET":
        form.title_pl.data = (mod.title or {}).get("pl", "")
        form.title_en.data = (mod.title or {}).get("en", "")
        form.body_pl.data = (mod.body_md or {}).get("pl", "")
        form.body_en.data = (mod.body_md or {}).get("en", "")
    if form.validate_on_submit():
        mod.title = {
            "pl": form.title_pl.data.strip(),
            "en": form.title_en.data.strip(),
        }
        mod.body_md = {
            "pl": form.body_pl.data,
            "en": form.body_en.data,
        }
        audit.record(
            entity_type="training_module",
            entity_id=mod.id,
            action=AuditAction.UPDATE,
        )
        db.session.commit()
        flash(_("admin.training.module.updated"), "success")
        return redirect(url_for("admin_training.modules_index", course_id=course.id))
    return render_template(
        "admin/training/module_form.html",
        course=course,
        version=version,
        form=form,
        module=mod,
        edit=True,
    )


@bp.route(
    "/courses/<course_id>/modules/<module_id>/delete", methods=["POST"]
)
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def modules_delete(course_id: str, module_id: str):
    course, version = _resolve_active_version(course_id)
    mod = db.session.get(TrainingModule, module_id)
    if mod is None or mod.course_version_id != version.id:
        abort(404)
    audit.record(
        entity_type="training_module",
        entity_id=mod.id,
        action="delete",
    )
    db.session.delete(mod)
    db.session.commit()
    flash(_("admin.training.module.deleted"), "success")
    return redirect(url_for("admin_training.modules_index", course_id=course.id))


# ─── Questions ──────────────────────────────────────────────────────


@bp.route("/courses/<course_id>/questions")
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def questions_index(course_id: str):
    course, version = _resolve_active_version(course_id)
    return render_template(
        "admin/training/questions_list.html",
        course=course,
        version=version,
        questions=version.questions,
    )


def _save_options(question: TrainingQuestion, payload: list[tuple[dict, bool]]) -> None:
    """Replace the question's options atomically with `payload`."""
    for old in list(question.options):
        db.session.delete(old)
    db.session.flush()
    for idx, (label, is_correct) in enumerate(payload):
        db.session.add(
            TrainingAnswerOption(
                question_id=question.id,
                order_index=idx,
                label=label,
                is_correct=is_correct,
            )
        )


@bp.route("/courses/<course_id>/questions/new", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def questions_new(course_id: str):
    course, version = _resolve_active_version(course_id)
    form = QuestionForm()
    if form.validate_on_submit():
        opts = form.options_payload()
        if len(opts) < 2:
            flash(_("admin.training.question.need_two_options"), "danger")
        elif not any(c for _, c in opts):
            flash(_("admin.training.question.need_correct"), "danger")
        else:
            next_idx = 1 + max(
                (q.order_index for q in version.questions), default=-1
            )
            q = TrainingQuestion(
                course_version_id=version.id,
                order_index=next_idx,
                prompt={
                    "pl": form.prompt_pl.data.strip(),
                    "en": form.prompt_en.data.strip(),
                },
                kind=form.kind.data,
            )
            db.session.add(q)
            db.session.flush()
            _save_options(q, opts)
            audit.record(
                entity_type="training_question",
                entity_id=q.id,
                action=AuditAction.CREATE,
                diff={"course_version_id": version.id, "n_options": len(opts)},
            )
            db.session.commit()
            flash(_("admin.training.question.created"), "success")
            return redirect(
                url_for("admin_training.questions_index", course_id=course.id)
            )
    return render_template(
        "admin/training/question_form.html",
        course=course,
        version=version,
        form=form,
        edit=False,
    )


@bp.route(
    "/courses/<course_id>/questions/<question_id>", methods=["GET", "POST"]
)
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def questions_edit(course_id: str, question_id: str):
    course, version = _resolve_active_version(course_id)
    q = db.session.get(TrainingQuestion, question_id)
    if q is None or q.course_version_id != version.id:
        abort(404)
    form = QuestionForm()
    if request.method == "GET":
        form.prompt_pl.data = (q.prompt or {}).get("pl", "")
        form.prompt_en.data = (q.prompt or {}).get("en", "")
        form.kind.data = q.kind
        for slot, opt in zip((1, 2, 3, 4), q.options):
            getattr(form, f"opt{slot}_pl").data = (opt.label or {}).get("pl", "")
            getattr(form, f"opt{slot}_en").data = (opt.label or {}).get("en", "")
            getattr(form, f"opt{slot}_correct").data = bool(opt.is_correct)
    if form.validate_on_submit():
        opts = form.options_payload()
        if len(opts) < 2:
            flash(_("admin.training.question.need_two_options"), "danger")
        elif not any(c for _, c in opts):
            flash(_("admin.training.question.need_correct"), "danger")
        else:
            q.prompt = {
                "pl": form.prompt_pl.data.strip(),
                "en": form.prompt_en.data.strip(),
            }
            q.kind = form.kind.data
            _save_options(q, opts)
            audit.record(
                entity_type="training_question",
                entity_id=q.id,
                action=AuditAction.UPDATE,
            )
            db.session.commit()
            flash(_("admin.training.question.updated"), "success")
            return redirect(
                url_for("admin_training.questions_index", course_id=course.id)
            )
    return render_template(
        "admin/training/question_form.html",
        course=course,
        version=version,
        form=form,
        question=q,
        edit=True,
    )


@bp.route(
    "/courses/<course_id>/questions/<question_id>/delete", methods=["POST"]
)
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def questions_delete(course_id: str, question_id: str):
    course, version = _resolve_active_version(course_id)
    q = db.session.get(TrainingQuestion, question_id)
    if q is None or q.course_version_id != version.id:
        abort(404)
    audit.record(
        entity_type="training_question",
        entity_id=q.id,
        action="delete",
    )
    db.session.delete(q)
    db.session.commit()
    flash(_("admin.training.question.deleted"), "success")
    return redirect(url_for("admin_training.questions_index", course_id=course.id))


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
