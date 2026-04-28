from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required

from app.auth import require_permission
from app.services import tickets as ticket_service

bp = Blueprint("dashboard", __name__, template_folder="../templates")


@bp.route("/")
@login_required
@require_permission("dashboard.view")
def index():
    stats = ticket_service.stats_overview()
    recent = ticket_service.list_tickets(open_only=True, limit=10)
    return render_template("dashboard/index.html", stats=stats, recent_tickets=recent)
