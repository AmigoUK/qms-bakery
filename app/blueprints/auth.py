from __future__ import annotations

from flask import Blueprint, flash, make_response, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Length

from app.auth import authenticate
from app.extensions import db
from app.i18n import gettext as _
from app.services import audit

bp = Blueprint("auth", __name__, template_folder="../templates")


class LoginForm(FlaskForm):
    email = StringField("email", validators=[DataRequired(), Length(max=255)])
    password = PasswordField("password", validators=[DataRequired()])
    submit = SubmitField()


@bp.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    error = None
    if form.validate_on_submit():
        user = authenticate(form.email.data, form.password.data)
        if user:
            login_user(user)
            next_url = request.args.get("next") or url_for("dashboard.index")
            return redirect(next_url)
        error = _("auth.login.invalid")
    return render_template("auth/login.html", form=form, error=error)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    from flask_login import current_user

    audit.record(entity_type="user", entity_id=current_user.id, action="logout")
    db.session.commit()
    logout_user()
    flash(_("auth.logout.success"), "info")
    return redirect(url_for("auth.login"))


@bp.route("/lang/<code>", methods=["POST", "GET"])
def set_language(code: str):
    from flask import current_app, g
    from flask_login import current_user

    supported = current_app.config["SUPPORTED_LANGUAGES"]
    if code not in supported:
        return ("invalid language", 400)

    next_url = request.args.get("next") or request.referrer or url_for("dashboard.index")
    response = make_response(redirect(next_url))
    response.set_cookie("lang", code, max_age=60 * 60 * 24 * 365, samesite="Lax")
    if current_user.is_authenticated:
        current_user.language = code
        db.session.commit()
    g.lang = code
    return response
