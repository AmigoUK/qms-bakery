"""Admin pages for training: courses, trainees, dashboard.

Mounted under /admin/training/* and registered alongside admin_bp in
app/__init__.py. Permission gating uses TRAINING_AUTHOR (course
authoring), TRAINING_SEND (trainee CRUD + ad-hoc enrolment),
TRAINING_REVIEW (read-only dashboard + attempt detail).
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
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
    """`employee_number` is the stable HR identifier — required on
    create, editable later (audited). Phone is a delivery channel,
    not identity. Channel governs which lane(s) the magic link is
    queued on at issue time.
    """

    employee_number = StringField(
        "employee_number", validators=[Optional(), Length(max=32)]
    )
    phone = StringField("phone", validators=[DataRequired(), Length(max=32)])
    email = StringField(
        "email", validators=[Optional(), Length(max=255)]
    )
    full_name = StringField(
        "full_name", validators=[DataRequired(), Length(max=MAX_FULL_NAME_LENGTH)]
    )
    role_code = StringField(
        "role_code", validators=[DataRequired(), Length(max=32)]
    )
    line_code = SelectField("line_code", validators=[Optional()])
    language = SelectField("language")
    notification_channel = SelectField(
        "notification_channel",
        choices=[
            ("sms", "SMS"),
            ("email", "Email"),
            ("both", "SMS + Email"),
        ],
        default="sms",
    )
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
    """List of trainees with optional multi-select filters by role
    code and production line. Filters apply at the SQL layer so the
    bulk-action checkbox column always operates on a consistent
    subset (`select-all` selects only what's currently visible)."""
    role_codes = [x.strip() for x in request.args.getlist("role_code") if x.strip()]
    line_ids = [x for x in request.args.getlist("line_id") if x]

    query = Trainee.query
    if role_codes:
        query = query.filter(Trainee.role_code.in_(role_codes))
    if line_ids:
        query = query.filter(Trainee.line_id.in_(line_ids))
    rows = query.order_by(Trainee.full_name).all()

    # Choices for the filter UI: distinct role codes seen on any
    # trainee, plus the active production lines.
    role_choices = sorted({
        r[0] for r in db.session.query(Trainee.role_code).distinct().all() if r[0]
    })
    line_choices = (
        ProductionLine.query.filter_by(is_active=True)
        .order_by(ProductionLine.code).all()
    )
    line_by_id = {ln.id: ln.code for ln in line_choices}
    courses = (
        TrainingCourse.query.filter_by(is_active=True)
        .order_by(TrainingCourse.code).all()
    )
    return render_template(
        "admin/training/trainees_list.html",
        trainees=rows,
        role_choices=role_choices,
        line_choices=line_choices,
        line_by_id=line_by_id,
        courses=courses,
        filter_role_codes=set(role_codes),
        filter_line_ids=set(line_ids),
    )


@bp.route("/trainees/new", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainees_new():
    form = TraineeForm()
    form.line_code.choices = _line_choices()
    form.language.choices = _language_choices()
    if form.validate_on_submit():
        email = (form.email.data or "").strip()
        channel = form.notification_channel.data or "sms"
        emp_no = (form.employee_number.data or "").strip()
        # Both employee_number and email are required when adding a
        # new trainee — they're the new identity-of-record + the
        # alternative delivery channel.
        if not emp_no:
            flash(_("admin.training.trainee.employee_number_required"), "danger")
        elif not email:
            flash(_("admin.training.trainee.email_required_on_create"), "danger")
        elif channel != "sms" and not email:
            flash(_("admin.training.trainee.email_required_for_channel"), "danger")
        else:
            try:
                trainee = training_service.create_trainee(
                    employee_number=emp_no,
                    phone=form.phone.data.strip(),
                    email=email,
                    full_name=form.full_name.data.strip(),
                    role_code=form.role_code.data.strip(),
                    line_id=_resolve_line_id(form.line_code.data),
                    language=form.language.data,
                    notification_channel=channel,
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
        form.employee_number.data = trainee.employee_number or ""
        form.phone.data = trainee.phone
        form.email.data = trainee.email or ""
        form.full_name.data = trainee.full_name
        form.role_code.data = trainee.role_code
        form.is_active.data = trainee.is_active
        form.language.data = trainee.language
        form.notification_channel.data = trainee.notification_channel
        if trainee.line_id:
            line = db.session.get(ProductionLine, trainee.line_id)
            if line:
                form.line_code.data = line.code
    if form.validate_on_submit():
        prev = {
            "full_name": trainee.full_name,
            "role_code": trainee.role_code,
            "is_active": trainee.is_active,
            "notification_channel": trainee.notification_channel,
            "employee_number": trainee.employee_number,
        }
        new_email = (form.email.data or "").strip() or None
        new_channel = form.notification_channel.data or "sms"
        new_emp_no = (form.employee_number.data or "").strip() or None
        # Block channel=email/both without an email on file.
        if new_channel != "sms" and not new_email:
            flash(_("admin.training.trainee.email_required_for_channel"), "danger")
            return render_template(
                "admin/training/trainee_form.html", form=form, edit=True, trainee=trainee
            )
        # Reject collision against another trainee's employee_number.
        if new_emp_no and new_emp_no != trainee.employee_number:
            existing = (
                Trainee.query.filter(Trainee.employee_number == new_emp_no)
                .filter(Trainee.id != trainee.id).first()
            )
            if existing is not None:
                flash(
                    _("admin.training.trainee.employee_number_taken"),
                    "danger",
                )
                return render_template(
                    "admin/training/trainee_form.html",
                    form=form, edit=True, trainee=trainee,
                )
        trainee.employee_number = new_emp_no
        trainee.email = new_email
        trainee.full_name = form.full_name.data.strip()
        trainee.role_code = form.role_code.data.strip()
        trainee.language = form.language.data
        trainee.is_active = form.is_active.data
        trainee.line_id = _resolve_line_id(form.line_code.data)
        trainee.notification_channel = new_channel
        audit.record(
            entity_type="trainee",
            entity_id=trainee.id,
            action=AuditAction.UPDATE,
            diff={"before": prev, "channel": trainee.notification_channel},
        )
        db.session.commit()
        flash(_("admin.training.trainee.updated"), "success")
        return redirect(url_for("admin_training.trainees_index"))
    return render_template(
        "admin/training/trainee_form.html", form=form, edit=True, trainee=trainee
    )


@bp.route("/trainees/bulk-issue", methods=["POST"])
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainees_bulk_issue():
    """Bulk-issue magic-links for the checked trainees + chosen course.

    Two-pass: a POST without `apply=1` renders the confirm preview;
    a POST with `apply=1` re-validates and commits. Each issued
    enrolment is a fresh row with its own unique magic_token.
    """
    trainee_ids = request.form.getlist("trainee_ids")
    course_code = (request.form.get("course_code") or "").strip()
    if not trainee_ids:
        flash(_("admin.training.bulk.no_trainees"), "danger")
        return redirect(url_for("admin_training.trainees_index"))
    course = training_service.get_course_by_code(course_code)
    if course is None:
        flash(_("admin.training.bulk.no_course"), "danger")
        return redirect(url_for("admin_training.trainees_index"))

    # Re-plan on every pass so the apply step picks up enrolments
    # that landed between preview and apply.
    plan = training_service.plan_bulk_issue(trainee_ids, course)

    if request.form.get("apply") == "1":
        base_url = (
            current_app.config.get("TRAINING_BASE_URL")
            or request.host_url.rstrip("/")
        )
        counts = training_service.apply_bulk_issue(
            plan, base_url=base_url, by_user_id=current_user.id
        )
        db.session.commit()
        flash(
            _("admin.training.bulk.applied").format(**counts),
            "success",
        )
        return redirect(url_for("admin_training.trainees_index"))

    return render_template(
        "admin/training/trainees_bulk_confirm.html",
        plan=plan,
        course=course,
        trainee_ids=trainee_ids,
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
                from flask import current_app, request as _req

                base_url = (
                    current_app.config.get("TRAINING_BASE_URL")
                    or _req.host_url.rstrip("/")
                )
                enrolment = training_service.enrol(
                    trainee=trainee, course=course, base_url=base_url
                )
                db.session.commit()
                link = f"{base_url.rstrip('/')}/training/take/{enrolment.magic_token}"
                flash(
                    _("admin.training.link_issued") + " " + link,
                    "success",
                )
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


# ─── Assignments ────────────────────────────────────────────────────


@bp.route("/courses/<course_id>/assignments")
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def assignments_index(course_id: str):
    course = db.session.get(TrainingCourse, course_id)
    if course is None:
        abort(404)
    rows = list(course.assignments)
    line_by_id = {
        line.id: line.code
        for line in ProductionLine.query.all()
    }
    return render_template(
        "admin/training/assignments_list.html",
        course=course,
        assignments=rows,
        line_by_id=line_by_id,
    )


@bp.route("/courses/<course_id>/assignments/new", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def assignments_new(course_id: str):
    course = db.session.get(TrainingCourse, course_id)
    if course is None:
        abort(404)
    form = AssignmentForm()
    form.line_code.choices = _line_choices()
    if form.validate_on_submit():
        role_code = (form.role_code.data or "").strip() or None
        line_id = _resolve_line_id(form.line_code.data)
        assignment = TrainingAssignment(
            course_id=course.id,
            role_code=role_code,
            line_id=line_id,
            recurrence_months=int(form.recurrence_months.data),
        )
        db.session.add(assignment)
        db.session.flush()
        audit.record(
            entity_type="training_assignment",
            entity_id=assignment.id,
            action=AuditAction.CREATE,
            diff={
                "course_id": course.id,
                "role_code": role_code,
                "line_id": line_id,
                "recurrence_months": assignment.recurrence_months,
            },
        )
        db.session.commit()
        flash(_("admin.training.assignment.created"), "success")
        return redirect(
            url_for("admin_training.assignments_index", course_id=course.id)
        )
    return render_template(
        "admin/training/assignment_form.html",
        course=course,
        form=form,
    )


@bp.route(
    "/courses/<course_id>/assignments/<assignment_id>/delete", methods=["POST"]
)
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def assignments_delete(course_id: str, assignment_id: str):
    course = db.session.get(TrainingCourse, course_id)
    if course is None:
        abort(404)
    assignment = db.session.get(TrainingAssignment, assignment_id)
    if assignment is None or assignment.course_id != course.id:
        abort(404)
    audit.record(
        entity_type="training_assignment",
        entity_id=assignment.id,
        action="delete",
        diff={
            "course_id": course.id,
            "role_code": assignment.role_code,
            "line_id": assignment.line_id,
        },
    )
    db.session.delete(assignment)
    db.session.commit()
    flash(_("admin.training.assignment.deleted"), "success")
    return redirect(
        url_for("admin_training.assignments_index", course_id=course.id)
    )


# ─── CSV export / template / import — trainees + courses ──────────


def _csv_response(payload: bytes, filename: str):
    from flask import Response

    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/trainees.csv")
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainees_export_csv():
    from app.services import training_csv

    from datetime import date as _date

    return _csv_response(
        training_csv.export_trainees(),
        f"trainees-{_date.today().isoformat()}.csv",
    )


@bp.route("/trainees/template.csv")
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainees_template_csv():
    from app.services import training_csv

    return _csv_response(training_csv.trainees_template(), "trainees-template.csv")


@bp.route("/trainees/import", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainees_import():
    """Two-pass CSV upload: dry-run preview by default, ?apply=1 commits.

    The plan is recomputed on each upload — we don't persist it
    between requests, which means the user re-uploads the same file
    when moving from preview to apply. That keeps the flow stateless
    and dodges the Redis-down failure mode dev/test sees.
    """
    from app.services import training_csv

    plan: training_csv.TraineeImportPlan | None = None
    if request.method == "POST":
        upload = request.files.get("csv")
        if upload is None or not upload.filename:
            flash(_("admin.training.import.no_file"), "danger")
        else:
            blob = upload.read()
            plan = training_csv.plan_trainees_import(blob)
            if request.form.get("apply") == "1":
                if plan.n_error == 0 and (plan.n_create + plan.n_update) > 0:
                    counts = training_csv.apply_trainees_plan(plan)
                    db.session.commit()
                    flash(
                        _("admin.training.import.applied").format(
                            created=counts["created"],
                            updated=counts["updated"],
                            skipped=counts["skipped"],
                        ),
                        "success",
                    )
                    return redirect(url_for("admin_training.trainees_index"))
                else:
                    flash(_("admin.training.import.fix_errors_first"), "danger")
    return render_template(
        "admin/training/trainees_import.html",
        plan=plan,
    )


@bp.route("/courses.csv")
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def courses_export_csv():
    from app.services import training_csv

    from datetime import date as _date

    return _csv_response(
        training_csv.export_courses(),
        f"courses-{_date.today().isoformat()}.csv",
    )


@bp.route("/courses/template.csv")
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def courses_template_csv():
    from app.services import training_csv

    return _csv_response(training_csv.courses_template(), "courses-template.csv")


@bp.route("/courses/import", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRAINING_AUTHOR)
def courses_import():
    from app.services import training_csv

    plan: training_csv.CourseImportPlan | None = None
    if request.method == "POST":
        upload = request.files.get("csv")
        if upload is None or not upload.filename:
            flash(_("admin.training.import.no_file"), "danger")
        else:
            blob = upload.read()
            plan = training_csv.plan_courses_import(blob)
            if request.form.get("apply") == "1":
                if plan.n_error == 0 and (plan.n_create + plan.n_update) > 0:
                    counts = training_csv.apply_courses_plan(plan)
                    db.session.commit()
                    flash(
                        _("admin.training.import.applied").format(
                            created=counts["created"],
                            updated=counts["updated"],
                            skipped=counts["skipped"],
                        ),
                        "success",
                    )
                    return redirect(url_for("admin_training.courses_index"))
                else:
                    flash(_("admin.training.import.fix_errors_first"), "danger")
    return render_template(
        "admin/training/courses_import.html",
        plan=plan,
    )


# ─── Matrix exports (CSV + PDF) ─────────────────────────────────────


def _matrix_export_rows(matrix: dict, line_by_id: dict) -> list[list[str]]:
    """Flatten the matrix into rows suitable for both CSV and PDF.

    First row is header (Worker / Role / Line / Clearance / one column
    per course code). Remaining rows are one per trainee with the
    state for each course rendered as its enum value.
    """
    header = [
        "Worker",
        "Role",
        "Line",
        "Clearance",
    ] + [c.code for c in matrix["courses"]]
    rows: list[list[str]] = [header]
    for trainee in matrix["trainees"]:
        cleared = matrix["cleared"].get(trainee.id, True)
        line = line_by_id.get(trainee.line_id, "") if trainee.line_id else ""
        cells = []
        for course in matrix["courses"]:
            state = matrix["states"].get((trainee.id, course.id))
            cert = matrix["certs"].get((trainee.id, course.id))
            text = state.value if state else ""
            if cert and state in (
                training_service.ComplianceState.VALID,
                training_service.ComplianceState.DUE_SOON,
                training_service.ComplianceState.EXTRA,
            ):
                text = f"{state.value} (until {cert.valid_until:%Y-%m-%d})"
            cells.append(text)
        rows.append([
            trainee.full_name,
            trainee.role_code,
            line,
            "cleared" if cleared else "BLOCKED",
        ] + cells)
    return rows


@bp.route("/dashboard.csv")
@login_required
@require_permission(Perm.TRAINING_REVIEW)
def dashboard_csv():
    """CSV export of the (filtered) compliance matrix.

    Same query parameters as the on-screen dashboard so an auditor
    can export exactly what they're looking at. RFC 4180 dialect via
    csv.writer; UTF-8 with BOM so Excel opens it cleanly without
    asking about encoding.
    """
    import csv
    import io

    line_ids = [x for x in request.args.getlist("line_id") if x]
    role_codes = [x.strip() for x in request.args.getlist("role_code") if x.strip()]
    blocked_only = request.args.get("blocked") == "1"
    matrix = training_service.build_compliance_matrix(
        line_ids=line_ids, role_codes=role_codes, blocked_only=blocked_only
    )
    line_by_id = {ln.id: ln.code for ln in ProductionLine.query.all()}
    rows = _matrix_export_rows(matrix, line_by_id)

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM for Excel
    writer = csv.writer(buf)
    writer.writerows(rows)
    payload = buf.getvalue().encode("utf-8")

    from flask import Response
    from datetime import date as _date

    filename = f"compliance-matrix-{_date.today().isoformat()}.csv"
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/dashboard.pdf")
@login_required
@require_permission(Perm.TRAINING_REVIEW)
def dashboard_pdf():
    """PDF export of the (filtered) compliance matrix.

    Renders a print-tuned HTML template through WeasyPrint (already
    used by the existing reports.* routes for HACCP / FSA traceability).
    Filename includes today's date so the inspector can collect them
    chronologically.
    """
    import weasyprint
    from datetime import date as _date

    line_ids = [x for x in request.args.getlist("line_id") if x]
    role_codes = [x.strip() for x in request.args.getlist("role_code") if x.strip()]
    blocked_only = request.args.get("blocked") == "1"
    # Orientation: explicit query override wins; otherwise auto-flip
    # to landscape when the matrix has more than 5 course columns
    # (portrait gets cramped past that).
    orient_param = (request.args.get("orient") or "").strip().lower()
    matrix = training_service.build_compliance_matrix(
        line_ids=line_ids, role_codes=role_codes, blocked_only=blocked_only
    )
    if orient_param in ("landscape", "portrait"):
        orientation = orient_param
    else:
        orientation = "landscape" if len(matrix["courses"]) > 5 else "portrait"
    line_by_id = {ln.id: ln.code for ln in ProductionLine.query.all()}

    html = render_template(
        "reports/training_compliance_matrix.html",
        matrix=matrix,
        line_by_id=line_by_id,
        ComplianceState=training_service.ComplianceState,
        generated_at=datetime.now(timezone.utc),
        filter_line_ids=line_ids,
        filter_role_codes=role_codes,
        filter_blocked_only=blocked_only,
        orientation=orientation,
    )
    pdf = weasyprint.HTML(
        string=html, base_url=str(current_app.root_path)
    ).write_pdf()
    from flask import Response

    filename = f"compliance-matrix-{_date.today().isoformat()}.pdf"
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ─── Drill-down: trainee × course history ──────────────────────────


@bp.route("/trainees/<trainee_id>/courses/<course_id>/history")
@login_required
@require_permission(Perm.TRAINING_REVIEW)
def trainee_course_history(trainee_id: str, course_id: str):
    """Audit-grade view of every interaction this trainee has had
    with this course: assignments matching, all enrolments (with
    status + token expiry), all attempts (score + payload), all
    declarations, all certifications (incl. revoked)."""
    trainee = db.session.get(Trainee, trainee_id)
    course = db.session.get(TrainingCourse, course_id)
    if trainee is None or course is None:
        abort(404)

    # Enrolments — newest first; eager-load attempts via the relation.
    enrolments = (
        db.session.execute(
            select(TrainingEnrolment)
            .join(
                TrainingCourseVersion,
                TrainingEnrolment.course_version_id == TrainingCourseVersion.id,
            )
            .where(TrainingEnrolment.trainee_id == trainee.id)
            .where(TrainingCourseVersion.course_id == course.id)
            .order_by(TrainingEnrolment.issued_at.desc())
        )
        .scalars()
        .all()
    )
    enrolment_ids = [e.id for e in enrolments]

    attempts = []
    if enrolment_ids:
        attempts = (
            TrainingAttempt.query.filter(TrainingAttempt.enrolment_id.in_(enrolment_ids))
            .order_by(TrainingAttempt.started_at.desc())
            .all()
        )

    certs = (
        TrainingCertification.query.filter_by(
            trainee_id=trainee.id, course_id=course.id
        )
        .order_by(TrainingCertification.valid_until.desc())
        .all()
    )

    line_code = None
    if trainee.line_id:
        line = db.session.get(ProductionLine, trainee.line_id)
        line_code = line.code if line else None

    can_issue = current_user.has_permission(Perm.TRAINING_SEND)
    return render_template(
        "admin/training/trainee_course_history.html",
        trainee=trainee,
        course=course,
        enrolments=enrolments,
        attempts=attempts,
        certs=certs,
        line_code=line_code,
        can_issue=can_issue,
    )


@bp.route(
    "/trainees/<trainee_id>/courses/<course_id>/issue", methods=["POST"]
)
@login_required
@require_permission(Perm.TRAINING_SEND)
def trainee_course_issue(trainee_id: str, course_id: str):
    """Issue a magic-link for this trainee × course straight from the
    drill-down history. Reuses the standard `enrol()` path so the SMS /
    email channel preference + audit trail behave identically to the
    Trainees > Issue form."""
    trainee = db.session.get(Trainee, trainee_id)
    course = db.session.get(TrainingCourse, course_id)
    if trainee is None or course is None:
        abort(404)
    base_url = (
        current_app.config.get("TRAINING_BASE_URL")
        or request.host_url.rstrip("/")
    )
    try:
        enrolment = training_service.enrol(
            trainee=trainee, course=course, base_url=base_url
        )
        db.session.commit()
        link = f"{base_url.rstrip('/')}/training/take/{enrolment.magic_token}"
        flash(_("admin.training.link_issued") + " " + link, "success")
    except training_service.TrainingError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(
        url_for(
            "admin_training.trainee_course_history",
            trainee_id=trainee.id,
            course_id=course.id,
        )
    )


# ─── Dashboard ──────────────────────────────────────────────────────


@bp.route("/dashboard")
@login_required
@require_permission(Perm.TRAINING_REVIEW)
def dashboard():
    """Per-trainee × per-course compliance matrix.

    Uses the bulk-loader so a 100×10 grid still hits the DB only ~5
    times. Six colour-coded states surface "required vs not", "valid /
    due-soon / overdue" and an "in-flight" enrolment-pending state.
    Worker-level clearance flag lights up the row when any required
    cell is OVERDUE.

    Optional query parameters narrow the visible set:
      ?line_id=<id>   — only one production line
      ?role_code=<rc> — only one role
      ?blocked=1      — only workers not currently cleared
    """
    # Multi-select aware: getlist returns [] when the param is absent
    # or empty, which `build_compliance_matrix` treats as "no filter".
    line_ids = [x for x in request.args.getlist("line_id") if x]
    role_codes = [x.strip() for x in request.args.getlist("role_code") if x.strip()]
    blocked_only = request.args.get("blocked") == "1"

    matrix = training_service.build_compliance_matrix(
        line_ids=line_ids,
        role_codes=role_codes,
        blocked_only=blocked_only,
    )
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
    line_by_id = {ln.id: ln.code for ln in ProductionLine.query.all()}
    # Choices for the filter selects: lines + the distinct set of
    # role_codes actually present on any active trainee.
    line_choices = (
        ProductionLine.query.filter_by(is_active=True)
        .order_by(ProductionLine.code).all()
    )
    role_choices = sorted({
        row[0] for row in db.session.query(Trainee.role_code)
        .filter(Trainee.is_active.is_(True)).distinct().all()
        if row[0]
    })
    return render_template(
        "admin/training/dashboard.html",
        matrix=matrix,
        line_by_id=line_by_id,
        open_enrolments=open_enrolments,
        ComplianceState=training_service.ComplianceState,
        # Filter UI state — sets so the template can do `id in selected`.
        filter_line_ids=set(line_ids),
        filter_role_codes=set(role_codes),
        filter_blocked_only=blocked_only,
        line_choices=line_choices,
        role_choices=role_choices,
    )
