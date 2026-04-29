"""Report endpoints — stream PDFs back to the browser.

Both routes require `reports.generate`. Output is `application/pdf`
with a content-disposition that suggests a sensible filename so the
browser doesn't drop a `report.pdf` into Downloads.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from flask import Blueprint, Response, abort, request
from flask_login import login_required

from app.auth import require_permission
from app.services import reports as reports_service

bp = Blueprint("reports", __name__, template_folder="../templates")


def _pdf_response(payload: bytes, filename: str) -> Response:
    response = Response(payload, mimetype="application/pdf")
    response.headers["Content-Disposition"] = (
        f'inline; filename="{filename}"'
    )
    response.headers["Content-Length"] = str(len(payload))
    return response


def _parse_int(value: Any, *, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        abort(400, description=f"{name!r} must be an integer")


def _parse_date(value: Any, *, name: str) -> date:
    if not value:
        abort(400, description=f"{name!r} is required (YYYY-MM-DD)")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        abort(400, description=f"{name!r} must be YYYY-MM-DD")


@bp.route("/haccp/monthly")
@login_required
@require_permission("reports.generate")
def haccp_monthly() -> Response:
    today = date.today()
    year = _parse_int(request.args.get("year", today.year), name="year")
    month = _parse_int(request.args.get("month", today.month), name="month")
    if not (1 <= month <= 12):
        abort(400, description="'month' must be in 1..12")
    line_id = request.args.get("line_id") or None

    pdf = reports_service.haccp_monthly_pdf(year, month, line_id)
    filename = f"haccp-monthly-{year:04d}-{month:02d}.pdf"
    return _pdf_response(pdf, filename)


@bp.route("/fsa/traceability")
@login_required
@require_permission("reports.generate")
def fsa_traceability() -> Response:
    date_from = _parse_date(request.args.get("from"), name="from")
    date_to = _parse_date(request.args.get("to"), name="to")
    if date_to < date_from:
        abort(400, description="'to' must be on or after 'from'")

    pdf = reports_service.fsa_traceability_pdf(date_from, date_to)
    filename = f"fsa-traceability-{date_from}_{date_to}.pdf"
    return _pdf_response(pdf, filename)
