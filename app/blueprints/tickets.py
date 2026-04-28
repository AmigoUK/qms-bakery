from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length

from app.auth import require_permission
from app.extensions import db
from app.i18n import gettext as _
from app.models import Ticket, TicketCategory, TicketSeverity, TicketStatus
from app.models.production import ProductionLine
from app.services import tickets as ticket_service

bp = Blueprint("tickets", __name__, template_folder="../templates")


def _line_choices() -> list[tuple[str, str]]:
    return [
        (line.id, f"{line.code} — {line.name}")
        for line in ProductionLine.query.filter_by(is_active=True).order_by(ProductionLine.code)
    ]


class CreateTicketForm(FlaskForm):
    line_id = SelectField("line_id", validators=[DataRequired()])
    title = StringField("title", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("description", validators=[Length(max=5000)])
    severity = SelectField(
        "severity",
        choices=[(s.value, s.value) for s in TicketSeverity],
        default=TicketSeverity.MEDIUM.value,
    )
    category = SelectField(
        "category",
        choices=[(c.value, c.value) for c in TicketCategory],
        default=TicketCategory.OTHER.value,
    )
    submit = SubmitField()


class CommentForm(FlaskForm):
    comment = TextAreaField("comment", validators=[DataRequired(), Length(max=5000)])
    submit = SubmitField()


@bp.route("/", methods=["GET"])
@login_required
@require_permission("tickets.view")
def index():
    open_only = request.args.get("open") == "1"
    severity = request.args.get("severity") or None
    line_id = request.args.get("line_id") or None
    rows = ticket_service.list_tickets(
        open_only=open_only, severity=severity, line_id=line_id
    )
    return render_template("tickets/list.html", tickets=rows, open_only=open_only)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("tickets.create")
def create():
    form = CreateTicketForm()
    form.line_id.choices = _line_choices()
    if form.validate_on_submit():
        try:
            ticket = ticket_service.create_ticket(
                line_id=form.line_id.data,
                title=form.title.data,
                description=form.description.data,
                severity=TicketSeverity(form.severity.data),
                category=TicketCategory(form.category.data),
                created_by_user_id=current_user.id,
                description_lang=getattr(request, "args", {}).get("lang") or current_user.language,
            )
            db.session.commit()
            flash(_("tickets.create.success", number=ticket.ticket_number), "success")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))
        except ticket_service.TicketError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template("tickets/create.html", form=form)


@bp.route("/<ticket_id>", methods=["GET", "POST"])
@login_required
@require_permission("tickets.view")
def detail(ticket_id: str):
    ticket = db.session.get(Ticket, ticket_id)
    if ticket is None:
        abort(404)
    comment_form = CommentForm()
    if comment_form.validate_on_submit():
        if not current_user.has_permission("tickets.classify"):
            abort(403)
        ticket_service.add_comment(
            ticket, user_id=current_user.id, comment=comment_form.comment.data
        )
        db.session.commit()
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    return render_template("tickets/detail.html", ticket=ticket, comment_form=comment_form)


@bp.route("/<ticket_id>/transition", methods=["POST"])
@login_required
@require_permission("tickets.classify")
def transition(ticket_id: str):
    ticket = db.session.get(Ticket, ticket_id)
    if ticket is None:
        abort(404)
    target = request.form.get("status", "")
    try:
        new_status = TicketStatus(target)
    except ValueError:
        abort(400)

    if new_status == TicketStatus.CLOSED and not current_user.has_permission("tickets.close"):
        abort(403)

    try:
        ticket_service.transition(
            ticket, new_status, user_id=current_user.id, comment=request.form.get("comment")
        )
        db.session.commit()
    except ticket_service.TicketError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("tickets.detail", ticket_id=ticket.id))
