"""Admin panel - read-only summary + key admin actions.

Scope is intentionally narrow: a single overview page plus user/CCP/SALSA/
trigger management entry points that reuse existing blueprints. Anything
write-heavy still goes through the domain blueprints (ccp.measure, salsa.fill,
etc.) to keep audit semantics consistent.
"""

from __future__ import annotations

import json
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import (
    BooleanField,
    DecimalField,
    IntegerField,
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.auth import hash_password, require_permission
from app.audit_actions import AuditAction
from app.constants import (
    ADMIN_RECENT_AUDIT_LIMIT,
    MAX_CODE_LENGTH,
    MAX_EMAIL_LENGTH,
    MAX_FULL_NAME_LENGTH,
    MAX_NAME_BILINGUAL_LENGTH,
    MAX_PASSWORD_LENGTH,
    TRIGGER_DURATION_MAX_SECONDS,
)
from app.permissions import Perm
from app.extensions import db
from app.i18n import gettext as _
from app.i18n import language_choices
from app.models import (
    AuditLog,
    CCPDefinition,
    Permission,
    Pipeline,
    PipelineStage,
    ProductionLine,
    Role,
    SalsaChecklist,
    Trigger,
    User,
)
from app.models.triggers import Responder, trigger_responders
from app.services import audit

bp = Blueprint("admin", __name__, template_folder="../templates")


@bp.route("/")
@login_required
@require_permission(Perm.SYSTEM_CONFIGURE)
def index():
    counts = {
        "users": User.query.count(),
        "lines": ProductionLine.query.count(),
        "ccps": CCPDefinition.query.count(),
        "salsa": SalsaChecklist.query.count(),
        "triggers": Trigger.query.count(),
        "pipelines": Pipeline.query.filter_by(is_active=True).count(),
        "audit_entries": AuditLog.query.count(),
    }
    recent_audit = (
        db.session.execute(
            select(AuditLog)
            .order_by(AuditLog.id.desc())
            .limit(ADMIN_RECENT_AUDIT_LIMIT)
        )
        .scalars()
        .all()
    )
    from datetime import date

    today = date.today()
    return render_template(
        "admin/index.html",
        counts=counts,
        recent_audit=recent_audit,
        today_year=today.year,
        today_month=today.month,
        today_iso=today.isoformat(),
        today_month_first=today.replace(day=1).isoformat(),
    )


# ─── Users ───────────────────────────────────────────────────────────────


class UserForm(FlaskForm):
    email = StringField(
        "email", validators=[DataRequired(), Length(max=MAX_EMAIL_LENGTH)]
    )
    full_name = StringField(
        "full_name", validators=[DataRequired(), Length(max=MAX_FULL_NAME_LENGTH)]
    )
    role_code = SelectField("role_code", validators=[DataRequired()])
    # Choices populated at request time from SUPPORTED_LANGUAGES.
    language = SelectField("language")
    password = StringField(
        "password", validators=[Length(min=0, max=MAX_PASSWORD_LENGTH)]
    )
    is_active = BooleanField("is_active", default=True)
    submit = SubmitField()


def _role_choices() -> list[tuple[str, str]]:
    return [(r.code, r.code) for r in Role.query.order_by(Role.code)]


def _language_choices() -> list[tuple[str, str]]:
    from flask import current_app

    return language_choices(tuple(current_app.config["SUPPORTED_LANGUAGES"]))


@bp.route("/users")
@login_required
@require_permission(Perm.USERS_MANAGE)
def users_index():
    rows = User.query.order_by(User.email).all()
    return render_template("admin/users_list.html", users=rows)


@bp.route("/users/new", methods=["GET", "POST"])
@login_required
@require_permission(Perm.USERS_MANAGE)
def users_new():
    form = UserForm()
    form.role_code.choices = _role_choices()
    form.language.choices = _language_choices()
    if form.validate_on_submit():
        if not form.password.data or len(form.password.data) < 8:
            flash(_("admin.users.password_min"), "danger")
        elif User.query.filter_by(email=form.email.data.strip().lower()).first():
            flash(_("admin.users.email_taken"), "danger")
        else:
            role = Role.query.filter_by(code=form.role_code.data).first()
            user = User(
                email=form.email.data.strip().lower(),
                full_name=form.full_name.data.strip(),
                language=form.language.data,
                role_id=role.id,
                password_hash=hash_password(form.password.data),
                is_active_flag=form.is_active.data,
            )
            db.session.add(user)
            db.session.flush()
            audit.record(
                entity_type="user",
                entity_id=user.id,
                action=AuditAction.CREATE,
                # Email is reachable via the FK (entity_id → users.email);
                # storing it inline in the audit diff would mean it lives
                # forever even after a GDPR-Art.17 redaction request.
                diff={"role": role.code},
            )
            db.session.commit()
            flash(_("admin.users.created"), "success")
            return redirect(url_for("admin.users_index"))
    return render_template("admin/users_form.html", form=form, edit=False)


@bp.route("/users/<user_id>", methods=["GET", "POST"])
@login_required
@require_permission(Perm.USERS_MANAGE)
def users_edit(user_id: str):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    form = UserForm(obj=None)
    form.role_code.choices = _role_choices()
    form.language.choices = _language_choices()
    if request.method == "GET":
        form.email.data = user.email
        form.full_name.data = user.full_name
        form.role_code.data = user.role.code if user.role else ""
        form.language.data = user.language
        form.is_active.data = user.is_active_flag
    if form.validate_on_submit():
        # Audit diff carries only mutable, non-PII fields: role and
        # is_active. Email is the entity key and lives on the User row
        # itself; full_name/language are PII that's redactable separately.
        prev = {"role": user.role.code, "is_active": user.is_active_flag}
        user.full_name = form.full_name.data.strip()
        user.language = form.language.data
        user.is_active_flag = form.is_active.data
        new_role = Role.query.filter_by(code=form.role_code.data).first()
        if new_role:
            user.role_id = new_role.id
        if form.password.data:
            if len(form.password.data) < 8:
                flash(_("admin.users.password_min"), "danger")
                return render_template("admin/users_form.html", form=form, edit=True)
            user.password_hash = hash_password(form.password.data)
        audit.record(
            entity_type="user",
            entity_id=user.id,
            action=AuditAction.UPDATE,
            diff={
                "before": prev,
                "after": {"role": user.role.code, "is_active": user.is_active_flag},
            },
        )
        db.session.commit()
        flash(_("admin.users.updated"), "success")
        return redirect(url_for("admin.users_index"))
    return render_template("admin/users_form.html", form=form, edit=True, user=user)


# ─── Triggers (read + activate/deactivate) ───────────────────────────────


@bp.route("/triggers")
@login_required
@require_permission(Perm.TRIGGERS_DEFINE)
def triggers_index():
    rows = Trigger.query.order_by(Trigger.code).all()
    return render_template("admin/triggers_list.html", triggers=rows)


@bp.route("/triggers/<trigger_id>/toggle", methods=["POST"])
@login_required
@require_permission(Perm.TRIGGERS_DEFINE)
def trigger_toggle(trigger_id: str):
    trigger = db.session.get(Trigger, trigger_id)
    if trigger is None:
        abort(404)
    trigger.is_active = not trigger.is_active
    audit.record(
        entity_type="trigger",
        entity_id=trigger.id,
        action=AuditAction.TOGGLE_ACTIVE,
        diff={"is_active": trigger.is_active},
    )
    db.session.commit()
    return redirect(url_for("admin.triggers_index"))


# Curated metric list. The form also accepts free-text via metric_other so a
# new sensor type doesn't need a code change to wire a trigger for it.
TRIGGER_METRIC_CHOICES: list[tuple[str, str]] = [
    ("temperature", "temperature"),
    ("humidity", "humidity"),
    ("pressure", "pressure"),
    ("weight", "weight"),
    ("ph", "pH"),
    ("count", "count"),
    ("__other__", "(other — type below)"),
]

TRIGGER_OPERATOR_CHOICES: list[tuple[str, str]] = [
    (">", ">"),
    (">=", ">="),
    ("<", "<"),
    ("<=", "<="),
    ("==", "=="),
    ("!=", "!="),
]

# Derive severity choices from the canonical TicketSeverity enum so a
# new severity drops in here automatically.
from app.models.tickets import TicketSeverity

TRIGGER_SEVERITY_CHOICES: list[tuple[str, str]] = [
    (s.value, s.value) for s in TicketSeverity
]


class TriggerForm(FlaskForm):
    code = StringField("code", validators=[DataRequired(), Length(max=MAX_CODE_LENGTH)])
    name_pl = StringField(
        "name_pl",
        validators=[DataRequired(), Length(max=MAX_NAME_BILINGUAL_LENGTH)],
    )
    name_en = StringField(
        "name_en",
        validators=[DataRequired(), Length(max=MAX_NAME_BILINGUAL_LENGTH)],
    )
    scope = SelectField("scope", validators=[Optional()])
    metric = SelectField(
        "metric", choices=TRIGGER_METRIC_CHOICES, validators=[DataRequired()]
    )
    metric_other = StringField(
        "metric_other", validators=[Optional(), Length(max=MAX_CODE_LENGTH)]
    )
    operator = SelectField(
        "operator", choices=TRIGGER_OPERATOR_CHOICES, validators=[DataRequired()]
    )
    value = DecimalField("value", validators=[DataRequired()])
    duration_seconds = IntegerField(
        "duration_seconds",
        default=0,
        validators=[Optional(), NumberRange(min=0, max=TRIGGER_DURATION_MAX_SECONDS)],
    )
    severity = SelectField(
        "severity", choices=TRIGGER_SEVERITY_CHOICES, validators=[DataRequired()]
    )
    is_active = BooleanField("is_active", default=True)
    dry_run = BooleanField("dry_run", default=False)
    responder_ids = SelectMultipleField("responder_ids", validators=[Optional()])
    submit = SubmitField()


def _populate_trigger_form_choices(form: TriggerForm) -> None:
    line_codes = [
        ("", "(none)"),
        *[
            (f"line:{code}", f"line:{code}")
            for (code,) in db.session.execute(
                select(ProductionLine.code).order_by(ProductionLine.code)
            ).all()
        ],
    ]
    form.scope.choices = line_codes
    form.responder_ids.choices = [
        (r.id, f"{r.code} ({r.type})")
        for r in Responder.query.order_by(Responder.code).all()
    ]


def _resolve_metric(form: TriggerForm) -> str:
    if form.metric.data == "__other__":
        return (form.metric_other.data or "").strip()
    return form.metric.data


def _trigger_to_form(trigger: Trigger, form: TriggerForm) -> None:
    """Populate a bound form from an existing Trigger row."""
    form.code.data = trigger.code
    form.name_pl.data = (trigger.name or {}).get("pl", "")
    form.name_en.data = (trigger.name or {}).get("en", "")
    form.scope.data = trigger.scope or ""
    cond = trigger.condition or {}
    metric = cond.get("metric", "")
    if metric in {c for c, _ in TRIGGER_METRIC_CHOICES}:
        form.metric.data = metric
    elif metric:
        form.metric.data = "__other__"
        form.metric_other.data = metric
    form.operator.data = cond.get("operator", ">")
    form.value.data = cond.get("value")
    form.duration_seconds.data = int(cond.get("duration_seconds") or 0)
    form.severity.data = trigger.severity
    form.is_active.data = trigger.is_active
    form.dry_run.data = trigger.dry_run
    form.responder_ids.data = [r.id for r in trigger.responders]


def _apply_form_to_trigger(form: TriggerForm, trigger: Trigger) -> None:
    """Copy validated form data into a Trigger row (without committing)."""
    trigger.code = form.code.data.strip()
    trigger.name = {"pl": form.name_pl.data.strip(), "en": form.name_en.data.strip()}
    trigger.scope = form.scope.data or None
    metric = _resolve_metric(form)
    condition: dict = {
        "metric": metric,
        "operator": form.operator.data,
        "value": float(form.value.data),
    }
    duration = int(form.duration_seconds.data or 0)
    if duration > 0:
        condition["duration_seconds"] = duration
    trigger.condition = condition
    trigger.severity = form.severity.data
    trigger.is_active = form.is_active.data
    trigger.dry_run = form.dry_run.data


def _sync_trigger_responders(trigger: Trigger, responder_ids: list[str]) -> None:
    """Replace the trigger ↔ responder associations preserving the order
    in which the IDs were submitted."""
    db.session.execute(
        trigger_responders.delete().where(
            trigger_responders.c.trigger_id == trigger.id
        )
    )
    for idx, rid in enumerate(responder_ids):
        if not rid:
            continue
        db.session.execute(
            trigger_responders.insert(),
            [{"trigger_id": trigger.id, "responder_id": rid, "order_index": idx}],
        )


@bp.route("/triggers/new", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRIGGERS_DEFINE)
def triggers_new():
    form = TriggerForm()
    _populate_trigger_form_choices(form)
    if form.validate_on_submit():
        if Trigger.query.filter_by(code=form.code.data.strip()).first():
            flash(_("admin.triggers.code_taken"), "danger")
        elif form.metric.data == "__other__" and not (form.metric_other.data or "").strip():
            flash(_("admin.triggers.metric_required"), "danger")
        else:
            trigger = Trigger(condition={})
            _apply_form_to_trigger(form, trigger)
            db.session.add(trigger)
            db.session.flush()
            _sync_trigger_responders(trigger, form.responder_ids.data or [])
            audit.record(
                entity_type="trigger",
                entity_id=trigger.id,
                action=AuditAction.CREATE,
                diff={
                    "code": trigger.code,
                    "scope": trigger.scope,
                    "condition": trigger.condition,
                },
            )
            db.session.commit()
            flash(_("admin.triggers.created"), "success")
            return redirect(url_for("admin.triggers_index"))
    return render_template("admin/triggers_form.html", form=form, edit=False)


@bp.route("/triggers/<trigger_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission(Perm.TRIGGERS_DEFINE)
def triggers_edit(trigger_id: str):
    trigger = db.session.get(Trigger, trigger_id)
    if trigger is None:
        abort(404)
    form = TriggerForm()
    _populate_trigger_form_choices(form)
    if request.method == "GET":
        _trigger_to_form(trigger, form)
    if form.validate_on_submit():
        existing = Trigger.query.filter_by(code=form.code.data.strip()).first()
        if existing and existing.id != trigger.id:
            flash(_("admin.triggers.code_taken"), "danger")
        elif form.metric.data == "__other__" and not (form.metric_other.data or "").strip():
            flash(_("admin.triggers.metric_required"), "danger")
        else:
            before = {
                "code": trigger.code,
                "scope": trigger.scope,
                "condition": trigger.condition,
                "severity": trigger.severity,
                "is_active": trigger.is_active,
                "dry_run": trigger.dry_run,
            }
            _apply_form_to_trigger(form, trigger)
            _sync_trigger_responders(trigger, form.responder_ids.data or [])
            audit.record(
                entity_type="trigger",
                entity_id=trigger.id,
                action=AuditAction.UPDATE,
                diff={
                    "before": before,
                    "after": {
                        "code": trigger.code,
                        "scope": trigger.scope,
                        "condition": trigger.condition,
                        "severity": trigger.severity,
                        "is_active": trigger.is_active,
                        "dry_run": trigger.dry_run,
                    },
                },
            )
            db.session.commit()
            flash(_("admin.triggers.updated"), "success")
            return redirect(url_for("admin.triggers_index"))
    return render_template(
        "admin/triggers_form.html", form=form, edit=True, trigger=trigger
    )


# ─── Audit trail (read-only) ─────────────────────────────────────────────


@bp.route("/audit")
@login_required
@require_permission(Perm.AUDIT_VIEW)
def audit_index():
    page_size = 100
    page = max(int(request.args.get("page", 1)), 1)
    q = (
        select(AuditLog)
        .order_by(AuditLog.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = db.session.execute(q).scalars().all()
    chain_ok, broken_id = audit.verify_chain()
    return render_template(
        "admin/audit_list.html",
        rows=rows,
        page=page,
        page_size=page_size,
        chain_ok=chain_ok,
        broken_id=broken_id,
    )


# ─── Pipelines ───────────────────────────────────────────────────────────

# Stage codes go into URLs and audit diffs - keep them ASCII-snake. Same shape
# as ProductionLine.code; 32 chars matches the column width.
_STAGE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _role_code_choices() -> list[tuple[str, str]]:
    """Roles available for stage `required_role_code`. Empty means 'any'."""
    return [("", "(any)")] + [(r.code, r.code) for r in Role.query.order_by(Role.code)]


class PipelineForm(FlaskForm):
    """Holds the JSON payload + CSRF token. Stage data lives in stages_json."""

    stages_json = StringField("stages_json", validators=[DataRequired()])
    submit = SubmitField()


def _serialise_pipeline(pipeline: Pipeline | None) -> list[dict]:
    if pipeline is None:
        return []
    return [
        {
            "code": s.code,
            "name_pl": (s.name or {}).get("pl", ""),
            "name_en": (s.name or {}).get("en", ""),
            "required_role_code": s.required_role_code or "",
            "sla_minutes": s.sla_minutes if s.sla_minutes is not None else "",
            "is_ccp_checkpoint": bool(s.is_ccp_checkpoint),
        }
        for s in pipeline.stages
    ]


def _validate_stages_payload(payload) -> tuple[list[dict] | None, str | None]:
    """Validate the parsed JSON. Returns (clean_stages, error_msg)."""
    if not isinstance(payload, list) or not payload:
        return None, "admin.pipelines.empty_stages"
    seen_codes: set[str] = set()
    clean: list[dict] = []
    valid_role_codes = {r.code for r in Role.query.all()}
    for raw in payload:
        if not isinstance(raw, dict):
            return None, "admin.pipelines.invalid_stage"
        code = (raw.get("code") or "").strip().lower()
        if not _STAGE_CODE_RE.match(code):
            return None, "admin.pipelines.invalid_code"
        if code in seen_codes:
            return None, "admin.pipelines.duplicate_code"
        seen_codes.add(code)
        name_pl = (raw.get("name_pl") or "").strip()
        name_en = (raw.get("name_en") or "").strip()
        if not name_pl or not name_en:
            return None, "admin.pipelines.missing_name"
        role_code = (raw.get("required_role_code") or "").strip() or None
        if role_code and role_code not in valid_role_codes:
            return None, "admin.pipelines.unknown_role"
        sla_raw = raw.get("sla_minutes")
        sla: int | None
        if sla_raw in (None, "", "null"):
            sla = None
        else:
            try:
                sla = int(sla_raw)
            except (TypeError, ValueError):
                return None, "admin.pipelines.invalid_sla"
            if sla < 0 or sla > 100000:
                return None, "admin.pipelines.invalid_sla"
        clean.append(
            {
                "code": code,
                "name": {"pl": name_pl, "en": name_en},
                "required_role_code": role_code,
                "sla_minutes": sla,
                "is_ccp_checkpoint": bool(raw.get("is_ccp_checkpoint")),
            }
        )
    return clean, None


@bp.route("/pipelines")
@login_required
@require_permission(Perm.PIPELINE_CONFIGURE)
def pipelines_index():
    lines = ProductionLine.query.order_by(ProductionLine.code).all()
    rows = []
    for line in lines:
        active = (
            Pipeline.query.filter_by(line_id=line.id, is_active=True)
            .order_by(Pipeline.version.desc())
            .first()
        )
        rows.append({"line": line, "pipeline": active})
    return render_template("admin/pipelines_list.html", rows=rows)


@bp.route("/pipelines/<line_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission(Perm.PIPELINE_CONFIGURE)
def pipelines_edit(line_id: str):
    line = db.session.get(ProductionLine, line_id)
    if line is None:
        abort(404)
    active = (
        Pipeline.query.filter_by(line_id=line.id, is_active=True)
        .order_by(Pipeline.version.desc())
        .first()
    )
    form = PipelineForm()

    if form.validate_on_submit():
        try:
            payload = json.loads(form.stages_json.data or "[]")
        except json.JSONDecodeError:
            flash(_("admin.pipelines.invalid_payload"), "danger")
            stages = json.loads(form.stages_json.data or "[]") if False else _serialise_pipeline(active)
            return render_template(
                "admin/pipelines_editor.html",
                form=form, line=line, pipeline=active,
                stages=stages, role_choices=_role_code_choices(),
            )
        clean_stages, err_key = _validate_stages_payload(payload)
        if err_key:
            flash(_(err_key), "danger")
            return render_template(
                "admin/pipelines_editor.html",
                form=form, line=line, pipeline=active,
                stages=payload if isinstance(payload, list) else _serialise_pipeline(active),
                role_choices=_role_code_choices(),
            )
        # Create new version. Old stays in DB (FKs from existing tickets) but
        # is_active=False — the engine reads only the active version.
        next_version = (
            db.session.query(db.func.max(Pipeline.version))
            .filter(Pipeline.line_id == line.id)
            .scalar()
            or 0
        ) + 1
        if active is not None:
            active.is_active = False
        new_pipeline = Pipeline(line_id=line.id, version=next_version, is_active=True)
        db.session.add(new_pipeline)
        db.session.flush()
        for idx, s in enumerate(clean_stages):
            db.session.add(
                PipelineStage(
                    pipeline_id=new_pipeline.id,
                    order_index=idx,
                    code=s["code"],
                    name=s["name"],
                    required_role_code=s["required_role_code"],
                    sla_minutes=s["sla_minutes"],
                    is_ccp_checkpoint=s["is_ccp_checkpoint"],
                )
            )
        audit.record(
            entity_type="pipeline",
            entity_id=new_pipeline.id,
            action=AuditAction.CREATE_VERSION,
            diff={
                "line_code": line.code,
                "version": next_version,
                "stage_codes": [s["code"] for s in clean_stages],
            },
        )
        db.session.commit()
        flash(_("admin.pipelines.saved"), "success")
        return redirect(url_for("admin.pipelines_index"))

    stages = _serialise_pipeline(active)
    return render_template(
        "admin/pipelines_editor.html",
        form=form,
        line=line,
        pipeline=active,
        stages=stages,
        role_choices=_role_code_choices(),
    )


# ─── DLQ (failed responder jobs) ─────────────────────────────────────────


@bp.route("/dlq")
@login_required
@require_permission(Perm.DLQ_MANAGE)
def dlq_index():
    from app.services import dlq as dlq_service

    rows = dlq_service.list_failed()
    counts = dlq_service.counts()
    return render_template("admin/dlq_list.html", rows=rows, counts=counts)


@bp.route("/dlq/<job_id>")
@login_required
@require_permission(Perm.DLQ_MANAGE)
def dlq_detail(job_id: str):
    from rq.exceptions import NoSuchJobError

    from app.services import dlq as dlq_service

    try:
        job, queue_name = dlq_service.get_failed(job_id)
    except NoSuchJobError:
        abort(404)
    return render_template(
        "admin/dlq_detail.html",
        job=job,
        queue_name=queue_name,
    )


@bp.route("/dlq/<job_id>/requeue", methods=["POST"])
@login_required
@require_permission(Perm.DLQ_MANAGE)
def dlq_requeue(job_id: str):
    from rq.exceptions import NoSuchJobError

    from app.services import dlq as dlq_service

    try:
        _job, queue_name = dlq_service.get_failed(job_id)
        dlq_service.requeue(job_id)
    except NoSuchJobError:
        abort(404)
    audit.record(
        entity_type="rq_job",
        entity_id=job_id,
        action=AuditAction.DLQ_REQUEUE,
        diff={"queue": queue_name},
    )
    db.session.commit()
    flash(_("admin.dlq.requeued"), "success")
    return redirect(url_for("admin.dlq_index"))


@bp.route("/dlq/<job_id>/discard", methods=["POST"])
@login_required
@require_permission(Perm.DLQ_MANAGE)
def dlq_discard(job_id: str):
    from rq.exceptions import NoSuchJobError

    from app.services import dlq as dlq_service

    try:
        queue_name = dlq_service.discard(job_id)
    except NoSuchJobError:
        abort(404)
    audit.record(
        entity_type="rq_job",
        entity_id=job_id,
        action=AuditAction.DLQ_DISCARD,
        diff={"queue": queue_name},
    )
    db.session.commit()
    flash(_("admin.dlq.discarded"), "success")
    return redirect(url_for("admin.dlq_index"))
