"""PDF report generation for FSA / HACCP compliance.

Two reports for now:

* `haccp_monthly` — every CCP measurement in the chosen calendar month,
  grouped by CCP, with a within-limits summary. The output is what an FSA
  inspector would expect to see during an audit: dates, values, who took
  the measurement, deviations highlighted.
* `fsa_traceability` — append-only audit log slice for a date range, with
  the chain-integrity verification stamp. Backs the "who did what when"
  question that comes up in any post-incident review.

Both functions return raw PDF bytes (`%PDF-…`) so callers can stream them
to a browser, attach to e-mail, or persist to disk.

Tests render to HTML first (`_render_html`) for cheap content assertions,
then verify the PDF byte stream is well-formed.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timezone
from typing import Any

from flask import current_app, render_template
from sqlalchemy import select

from app.extensions import db
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.haccp import CCPDefinition, CCPMeasurement
from app.models.production import ProductionLine
from app.services import audit as audit_service


def _render_html(template: str, **ctx: Any) -> str:
    return render_template(template, **ctx)


def _html_to_pdf(html: str) -> bytes:
    """Render a Jinja-produced HTML string into a PDF byte stream."""
    import weasyprint

    base_url = str(current_app.root_path)
    return weasyprint.HTML(string=html, base_url=base_url).write_pdf()


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    if not (1 <= month <= 12):
        raise ValueError(f"month out of range: {month}")
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return start, end


def haccp_monthly_context(
    year: int, month: int, line_id: str | None = None
) -> dict[str, Any]:
    """Collect the data structure the HACCP report template renders.

    Public so tests can assert on the bound data without needing to parse
    HTML or PDF output.
    """
    start, end = _month_bounds(year, month)

    line: ProductionLine | None = None
    if line_id:
        line = db.session.get(ProductionLine, line_id)

    ccp_query = select(CCPDefinition).where(CCPDefinition.is_active.is_(True))
    if line is not None:
        ccp_query = ccp_query.where(CCPDefinition.line_id == line.id)
    ccps = db.session.execute(ccp_query.order_by(CCPDefinition.code)).scalars().all()

    rows: list[dict[str, Any]] = []
    overall_total = 0
    overall_deviations = 0
    for ccp in ccps:
        measurements = (
            db.session.execute(
                select(CCPMeasurement)
                .where(
                    CCPMeasurement.ccp_id == ccp.id,
                    CCPMeasurement.measured_at >= start,
                    CCPMeasurement.measured_at <= end,
                )
                .order_by(CCPMeasurement.measured_at.asc())
            )
            .scalars()
            .all()
        )
        total = len(measurements)
        deviations = sum(1 for m in measurements if not m.is_within_limits)
        overall_total += total
        overall_deviations += deviations
        rows.append(
            {
                "ccp": ccp,
                "measurements": measurements,
                "total": total,
                "deviations": deviations,
                "within_limits_pct": (
                    round((total - deviations) / total * 100, 2) if total else None
                ),
            }
        )

    return {
        "year": year,
        "month": month,
        "month_name": calendar.month_name[month],
        "period_from": start,
        "period_to": end,
        "line": line,
        "rows": rows,
        "overall_total": overall_total,
        "overall_deviations": overall_deviations,
        "overall_within_pct": (
            round(
                (overall_total - overall_deviations) / overall_total * 100, 2
            )
            if overall_total
            else None
        ),
        "generated_at": datetime.now(timezone.utc),
    }


def haccp_monthly_html(
    year: int, month: int, line_id: str | None = None
) -> str:
    ctx = haccp_monthly_context(year, month, line_id)
    return _render_html("reports/haccp_monthly.html", **ctx)


def haccp_monthly_pdf(
    year: int, month: int, line_id: str | None = None
) -> bytes:
    return _html_to_pdf(haccp_monthly_html(year, month, line_id))


def fsa_traceability_context(
    date_from: date, date_to: date
) -> dict[str, Any]:
    if date_to < date_from:
        raise ValueError("date_to must be >= date_from")
    start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
    end = datetime.combine(date_to, time.max, tzinfo=timezone.utc)

    entries = (
        db.session.execute(
            select(AuditLog)
            .where(AuditLog.occurred_at >= start, AuditLog.occurred_at <= end)
            .order_by(AuditLog.id.asc())
        )
        .scalars()
        .all()
    )

    user_ids = {e.user_id for e in entries if e.user_id}
    users: dict[str, User] = (
        {
            u.id: u
            for u in db.session.execute(
                select(User).where(User.id.in_(user_ids))
            )
            .unique()
            .scalars()
        }
        if user_ids
        else {}
    )

    chain_ok, chain_broken_at = audit_service.verify_chain()

    return {
        "date_from": date_from,
        "date_to": date_to,
        "entries": entries,
        "users": users,
        "chain_ok": chain_ok,
        "chain_broken_at": chain_broken_at,
        "total_entries": len(entries),
        "generated_at": datetime.now(timezone.utc),
    }


def fsa_traceability_html(date_from: date, date_to: date) -> str:
    ctx = fsa_traceability_context(date_from, date_to)
    return _render_html("reports/fsa_traceability.html", **ctx)


def fsa_traceability_pdf(date_from: date, date_to: date) -> bytes:
    return _html_to_pdf(fsa_traceability_html(date_from, date_to))
