"""PDF report service + HTTP routes.

Service tests render to HTML for cheap content assertions and to PDF
bytes for a `%PDF-` magic-byte check. Route tests verify auth + the
Content-Type / Content-Disposition headers.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.extensions import db
from app.models.haccp import CCPDefinition
from app.models.production import ProductionLine
from app.services import audit as audit_service
from app.services import haccp as haccp_service
from app.services import reports as reports_service


def _seeded_ccp() -> CCPDefinition:
    return CCPDefinition.query.first()


# -- haccp_monthly_context ---------------------------------------------


def test_haccp_monthly_context_with_no_measurements(app):
    with app.app_context():
        ctx = reports_service.haccp_monthly_context(2026, 4)
        assert ctx["overall_total"] == 0
        assert ctx["overall_deviations"] == 0
        assert ctx["overall_within_pct"] is None
        # Seeded line has 2 CCPs (per README)
        assert len(ctx["rows"]) >= 1
        for row in ctx["rows"]:
            assert row["measurements"] == []
            assert row["total"] == 0


def test_haccp_monthly_context_includes_recorded_measurements(app):
    with app.app_context():
        ccp = _seeded_ccp()
        # In-spec measurement
        haccp_service.record_measurement(
            ccp_id=ccp.id,
            value=(ccp.critical_limit_min or 0) + 1.0,
            measured_by_user_id=None,
        )
        # Out-of-spec measurement (force deviation by exceeding upper limit)
        bad_value = (ccp.critical_limit_max or 0) + 50.0
        haccp_service.record_measurement(
            ccp_id=ccp.id, value=bad_value, measured_by_user_id=None
        )
        db.session.commit()

        now = datetime.now(timezone.utc)
        ctx = reports_service.haccp_monthly_context(now.year, now.month)
        assert ctx["overall_total"] == 2
        assert ctx["overall_deviations"] == 1
        assert ctx["overall_within_pct"] == 50.0


def test_haccp_monthly_context_filters_by_line(app):
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        ctx_filtered = reports_service.haccp_monthly_context(
            2026, 4, line_id=line.id
        )
        assert ctx_filtered["line"].id == line.id
        for row in ctx_filtered["rows"]:
            assert row["ccp"].line_id == line.id


def test_haccp_monthly_rejects_bad_month(app):
    with app.app_context():
        with pytest.raises(ValueError):
            reports_service.haccp_monthly_context(2026, 13)
        with pytest.raises(ValueError):
            reports_service.haccp_monthly_context(2026, 0)


# -- haccp_monthly HTML / PDF ------------------------------------------


def test_haccp_monthly_html_renders_period_and_summary(app):
    with app.app_context():
        html = reports_service.haccp_monthly_html(2026, 4)
        assert "April 2026" in html
        assert "HACCP monthly report" in html
        assert "Period" in html


def test_haccp_monthly_pdf_returns_pdf_bytes(app):
    with app.app_context():
        pdf = reports_service.haccp_monthly_pdf(2026, 4)
        assert pdf.startswith(b"%PDF-")
        # Reasonable lower bound — header + one page of content
        assert len(pdf) > 2000


# -- fsa_traceability ---------------------------------------------------


def test_fsa_traceability_includes_audit_entries_and_chain_status(app):
    with app.app_context():
        # Force at least one audit entry inside the window.
        audit_service.record(
            entity_type="report_test", action="ping", entity_id=None
        )
        db.session.commit()

        today = date.today()
        ctx = reports_service.fsa_traceability_context(today, today)
        assert ctx["chain_ok"] is True
        assert ctx["chain_broken_at"] is None
        assert ctx["total_entries"] >= 1


def test_fsa_traceability_html_shows_chain_verified(app):
    with app.app_context():
        today = date.today()
        html = reports_service.fsa_traceability_html(today, today)
        assert "FSA traceability report" in html
        assert "verified" in html.lower()  # chain_ok badge


def test_fsa_traceability_pdf_returns_pdf_bytes(app):
    with app.app_context():
        today = date.today()
        pdf = reports_service.fsa_traceability_pdf(today, today)
        assert pdf.startswith(b"%PDF-")


def test_fsa_traceability_rejects_inverted_range(app):
    with app.app_context():
        with pytest.raises(ValueError):
            reports_service.fsa_traceability_context(
                date(2026, 4, 30), date(2026, 4, 1)
            )


# -- HTTP routes -------------------------------------------------------


def test_haccp_monthly_route_redirects_when_anonymous(client):
    resp = client.get("/reports/haccp/monthly?year=2026&month=4")
    # login_required should redirect to /auth/login
    assert resp.status_code in (302, 401)
    if resp.status_code == 302:
        assert "/auth/login" in resp.headers.get("Location", "")


def test_haccp_monthly_route_returns_pdf_when_authorized(client, login_admin):
    login_admin()
    resp = client.get("/reports/haccp/monthly?year=2026&month=4")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF-")
    cd = resp.headers.get("Content-Disposition", "")
    assert "haccp-monthly-2026-04.pdf" in cd


def test_haccp_monthly_route_rejects_bad_month(client, login_admin):
    login_admin()
    resp = client.get("/reports/haccp/monthly?year=2026&month=13")
    assert resp.status_code == 400


def test_fsa_traceability_route_returns_pdf(client, login_admin):
    login_admin()
    resp = client.get("/reports/fsa/traceability?from=2026-01-01&to=2026-12-31")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF-")
    cd = resp.headers.get("Content-Disposition", "")
    assert "fsa-traceability-2026-01-01_2026-12-31.pdf" in cd


def test_fsa_traceability_route_rejects_inverted_range(client, login_admin):
    login_admin()
    resp = client.get("/reports/fsa/traceability?from=2026-12-31&to=2026-01-01")
    assert resp.status_code == 400


def test_fsa_traceability_route_rejects_bad_date_format(client, login_admin):
    login_admin()
    resp = client.get("/reports/fsa/traceability?from=2026/01/01&to=2026-12-31")
    assert resp.status_code == 400


def test_fsa_traceability_route_requires_from_param(client, login_admin):
    login_admin()
    resp = client.get("/reports/fsa/traceability?to=2026-12-31")
    assert resp.status_code == 400
