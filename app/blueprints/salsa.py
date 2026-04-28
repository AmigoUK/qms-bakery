from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth import require_permission
from app.extensions import db
from app.i18n import gettext as _
from app.models.salsa import SalsaChecklist
from app.services import salsa as salsa_service

bp = Blueprint("salsa", __name__, template_folder="../templates")


@bp.route("/")
@login_required
@require_permission("salsa.respond")
def index():
    checklists = salsa_service.list_checklists()
    return render_template("salsa/list.html", checklists=checklists)


@bp.route("/<checklist_id>", methods=["GET", "POST"])
@login_required
@require_permission("salsa.respond")
def fill(checklist_id: str):
    checklist = db.session.get(SalsaChecklist, checklist_id)
    if checklist is None or not checklist.is_active:
        abort(404)

    if request.method == "POST":
        answers: dict[str, dict] = {}
        for item in checklist.items:
            key = item["key"]
            answers[key] = {
                "ok": request.form.get(f"item__{key}__ok") == "yes",
                "comment": request.form.get(f"item__{key}__comment", ""),
            }
        try:
            response = salsa_service.submit_response(
                checklist_id=checklist.id, answers=answers, user_id=current_user.id
            )
            db.session.commit()
            if response.nonconformities_count == 0:
                flash(_("salsa.submit.ok"), "success")
            else:
                flash(_("salsa.submit.with_issues"), "warning")
            return redirect(url_for("salsa.index"))
        except salsa_service.SalsaError as exc:
            db.session.rollback()
            flash(str(exc), "danger")

    return render_template("salsa/fill.html", checklist=checklist)
