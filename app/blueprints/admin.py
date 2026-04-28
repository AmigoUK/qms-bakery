"""Admin panel - read-only summary + key admin actions.

Scope is intentionally narrow: a single overview page plus user/CCP/SALSA/
trigger management entry points that reuse existing blueprints. Anything
write-heavy still goes through the domain blueprints (ccp.measure, salsa.fill,
etc.) to keep audit semantics consistent.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import BooleanField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length

from app.auth import hash_password, require_permission
from app.extensions import db
from app.i18n import gettext as _
from app.models import (
    AuditLog,
    CCPDefinition,
    Permission,
    ProductionLine,
    Role,
    SalsaChecklist,
    Trigger,
    User,
)
from app.services import audit

bp = Blueprint("admin", __name__, template_folder="../templates")


@bp.route("/")
@login_required
@require_permission("system.configure")
def index():
    counts = {
        "users": User.query.count(),
        "lines": ProductionLine.query.count(),
        "ccps": CCPDefinition.query.count(),
        "salsa": SalsaChecklist.query.count(),
        "triggers": Trigger.query.count(),
        "audit_entries": AuditLog.query.count(),
    }
    recent_audit = (
        db.session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(20))
        .scalars()
        .all()
    )
    return render_template("admin/index.html", counts=counts, recent_audit=recent_audit)


# ─── Users ───────────────────────────────────────────────────────────────


class UserForm(FlaskForm):
    email = StringField("email", validators=[DataRequired(), Length(max=255)])
    full_name = StringField("full_name", validators=[DataRequired(), Length(max=120)])
    role_code = SelectField("role_code", validators=[DataRequired()])
    language = SelectField("language", choices=[("en", "English"), ("pl", "Polski")], default="en")
    password = StringField("password", validators=[Length(min=0, max=128)])
    is_active = BooleanField("is_active", default=True)
    submit = SubmitField()


def _role_choices() -> list[tuple[str, str]]:
    return [(r.code, r.code) for r in Role.query.order_by(Role.code)]


@bp.route("/users")
@login_required
@require_permission("users.manage")
def users_index():
    rows = User.query.order_by(User.email).all()
    return render_template("admin/users_list.html", users=rows)


@bp.route("/users/new", methods=["GET", "POST"])
@login_required
@require_permission("users.manage")
def users_new():
    form = UserForm()
    form.role_code.choices = _role_choices()
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
                action="create",
                diff={"email": user.email, "role": role.code},
            )
            db.session.commit()
            flash(_("admin.users.created"), "success")
            return redirect(url_for("admin.users_index"))
    return render_template("admin/users_form.html", form=form, edit=False)


@bp.route("/users/<user_id>", methods=["GET", "POST"])
@login_required
@require_permission("users.manage")
def users_edit(user_id: str):
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    form = UserForm(obj=None)
    form.role_code.choices = _role_choices()
    if request.method == "GET":
        form.email.data = user.email
        form.full_name.data = user.full_name
        form.role_code.data = user.role.code if user.role else ""
        form.language.data = user.language
        form.is_active.data = user.is_active_flag
    if form.validate_on_submit():
        prev = {"email": user.email, "role": user.role.code, "is_active": user.is_active_flag}
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
            action="update",
            diff={
                "before": prev,
                "after": {"email": user.email, "role": user.role.code, "is_active": user.is_active_flag},
            },
        )
        db.session.commit()
        flash(_("admin.users.updated"), "success")
        return redirect(url_for("admin.users_index"))
    return render_template("admin/users_form.html", form=form, edit=True, user=user)


# ─── Triggers (read + activate/deactivate) ───────────────────────────────


@bp.route("/triggers")
@login_required
@require_permission("triggers.define")
def triggers_index():
    rows = Trigger.query.order_by(Trigger.code).all()
    return render_template("admin/triggers_list.html", triggers=rows)


@bp.route("/triggers/<trigger_id>/toggle", methods=["POST"])
@login_required
@require_permission("triggers.define")
def trigger_toggle(trigger_id: str):
    trigger = db.session.get(Trigger, trigger_id)
    if trigger is None:
        abort(404)
    trigger.is_active = not trigger.is_active
    audit.record(
        entity_type="trigger",
        entity_id=trigger.id,
        action="toggle_active",
        diff={"is_active": trigger.is_active},
    )
    db.session.commit()
    return redirect(url_for("admin.triggers_index"))


# ─── Audit trail (read-only) ─────────────────────────────────────────────


@bp.route("/audit")
@login_required
@require_permission("audit.view")
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
