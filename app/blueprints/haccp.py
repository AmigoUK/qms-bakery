from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import FloatField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.auth import require_permission
from app.extensions import db
from app.i18n import gettext as _
from app.models.haccp import CCPDefinition
from app.services import haccp as haccp_service

bp = Blueprint("haccp", __name__, template_folder="../templates")


class MeasurementForm(FlaskForm):
    value = FloatField("value", validators=[DataRequired(), NumberRange(min=-1000, max=10000)])
    device_id = StringField("device_id", validators=[Optional(), Length(max=64)])
    notes = StringField("notes", validators=[Optional(), Length(max=500)])
    submit = SubmitField()


@bp.route("/")
@login_required
@require_permission("ccp.measure")
def index():
    ccps = haccp_service.list_definitions()
    return render_template("haccp/list.html", ccps=ccps)


@bp.route("/<ccp_id>", methods=["GET", "POST"])
@login_required
@require_permission("ccp.measure")
def detail(ccp_id: str):
    ccp = db.session.get(CCPDefinition, ccp_id)
    if ccp is None:
        abort(404)
    form = MeasurementForm()
    if form.validate_on_submit():
        try:
            measurement = haccp_service.record_measurement(
                ccp_id=ccp.id,
                value=form.value.data,
                measured_by_user_id=current_user.id,
                device_id=form.device_id.data or None,
                notes=form.notes.data or None,
            )
            db.session.commit()
            if measurement.is_within_limits:
                flash(_("haccp.measure.ok"), "success")
            else:
                flash(_("haccp.measure.deviation"), "danger")
            return redirect(url_for("haccp.detail", ccp_id=ccp.id))
        except haccp_service.HACCPError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    measurements = haccp_service.recent_measurements(ccp.id, limit=25)
    return render_template(
        "haccp/detail.html", ccp=ccp, form=form, measurements=measurements
    )
