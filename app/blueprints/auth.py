from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, flash, make_response, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Length

from app.auth import authenticate
from app.extensions import db
from app.i18n import gettext as _
from app.models import User
from app.services import audit
from app.services import totp as totp_service
from app.services.ratelimit import rate_limit

bp = Blueprint("auth", __name__, template_folder="../templates")


def _safe_next_url(candidate: str | None, fallback_endpoint: str = "dashboard.index") -> str:
    """Filter the `?next=` parameter to local paths only.

    A `next` URL with a scheme or netloc opens us to phishing redirects
    (`/auth/login?next=https://evil.example/qms`). Limit it to relative
    paths starting with `/` and not containing `//` (which Werkzeug treats
    as a netloc).
    """
    if not candidate:
        return url_for(fallback_endpoint)
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return url_for(fallback_endpoint)
    if not candidate.startswith("/") or candidate.startswith("//"):
        return url_for(fallback_endpoint)
    return candidate


class LoginForm(FlaskForm):
    email = StringField("email", validators=[DataRequired(), Length(max=255)])
    password = PasswordField("password", validators=[DataRequired()])
    submit = SubmitField()


class TOTPForm(FlaskForm):
    code = StringField("code", validators=[DataRequired(), Length(min=6, max=10)])
    submit = SubmitField()


@bp.route("/login", methods=["GET", "POST"])
@rate_limit(
    bucket="login",
    max_requests=10,
    window_seconds=60,
    config_max_key="RATELIMIT_LOGIN_MAX",
    config_window_key="RATELIMIT_LOGIN_WINDOW",
)
def login():
    form = LoginForm()
    error = None
    if form.validate_on_submit():
        user = authenticate(form.email.data, form.password.data)
        if user:
            if user.totp_enabled:
                # Stash pending login; complete after second factor.
                session["pending_user_id"] = user.id
                session["pending_next"] = _safe_next_url(request.args.get("next"))
                return redirect(url_for("auth.login_2fa"))
            login_user(user)
            return redirect(_safe_next_url(request.args.get("next")))
        error = _("auth.login.invalid")
    return render_template("auth/login.html", form=form, error=error)


@bp.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    pending_id = session.get("pending_user_id")
    if not pending_id:
        return redirect(url_for("auth.login"))
    user = db.session.get(User, pending_id)
    if user is None:
        session.pop("pending_user_id", None)
        return redirect(url_for("auth.login"))

    form = TOTPForm()
    error = None
    if form.validate_on_submit():
        if totp_service.verify_code(user, form.code.data):
            login_user(user)
            audit.record(
                entity_type="user", entity_id=user.id, action="login_2fa_success", user_id=user.id
            )
            db.session.commit()
            next_url = _safe_next_url(session.pop("pending_next", None))
            session.pop("pending_user_id", None)
            return redirect(next_url)
        audit.record(
            entity_type="user", entity_id=user.id, action="login_2fa_failure", user_id=user.id
        )
        db.session.commit()
        error = _("auth.2fa.invalid")
    return render_template("auth/login_2fa.html", form=form, error=error)


@bp.route("/2fa/enroll", methods=["GET", "POST"])
@login_required
def totp_enroll():
    user = current_user._get_current_object()
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if totp_service.complete_enrollment(user, code):
            audit.record(
                entity_type="user", entity_id=user.id, action="totp_enrolled", user_id=user.id
            )
            db.session.commit()
            flash(_("auth.2fa.enrolled"), "success")
            return redirect(url_for("dashboard.index"))
        flash(_("auth.2fa.invalid"), "danger")
        return redirect(url_for("auth.totp_enroll"))

    # GET: (re-)issue secret + provisioning URI.
    secret, uri = totp_service.begin_enrollment(user)
    db.session.commit()
    return render_template("auth/totp_enroll.html", secret=secret, uri=uri)


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

    candidate = request.args.get("next")
    if not candidate and request.referrer:
        # Strip referrer down to its path if it points at our own host.
        ref = urlparse(request.referrer)
        if ref.netloc == request.host:
            candidate = ref.path or "/"
    next_url = _safe_next_url(candidate)
    response = make_response(redirect(next_url))
    response.set_cookie("lang", code, max_age=60 * 60 * 24 * 365, samesite="Lax")
    if current_user.is_authenticated:
        current_user.language = code
        db.session.commit()
    g.lang = code
    return response
