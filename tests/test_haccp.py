"""HACCP / CCP module tests."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import CCPDefinition, CCPMeasurement, Ticket, TicketSeverity, TicketStatus
from app.models.production import ProductionLine
from app.services import audit
from app.services import haccp as haccp_service


def _ccp(parameter: str = "temperature", code: str = "CCP-OVEN-1") -> CCPDefinition:
    return CCPDefinition.query.filter_by(code=code).first()


def test_seed_creates_demo_ccps(app):
    assert CCPDefinition.query.count() >= 2
    ccp = _ccp()
    assert ccp.critical_limit_min == 180.0
    assert ccp.critical_limit_max == 220.0


def test_within_limits_helper(app):
    ccp = _ccp()
    assert ccp.is_within_limits(200.0) is True
    assert ccp.is_within_limits(180.0) is True
    assert ccp.is_within_limits(220.0) is True
    assert ccp.is_within_limits(179.9) is False
    assert ccp.is_within_limits(220.1) is False


def test_record_measurement_within_limits(app):
    ccp = _ccp()
    with app.test_request_context("/"):
        m = haccp_service.record_measurement(ccp_id=ccp.id, value=210.0)
        db.session.commit()
    assert m.is_within_limits is True
    assert m.linked_ticket_id is None
    # No ticket should have been created.
    assert Ticket.query.count() == 0


def test_record_measurement_deviation_creates_ticket(app):
    ccp = _ccp()
    with app.test_request_context("/"):
        m = haccp_service.record_measurement(
            ccp_id=ccp.id, value=235.0, device_id="oven-sensor-a"
        )
        db.session.commit()
    assert m.is_within_limits is False
    assert m.linked_ticket_id is not None
    ticket = db.session.get(Ticket, m.linked_ticket_id)
    assert ticket.severity == TicketSeverity.CRITICAL.value
    assert ticket.status == TicketStatus.NEW.value
    assert ticket.is_ccp_related is True
    assert ticket.extra_data["ccp_code"] == ccp.code
    assert ticket.extra_data["value"] == 235.0
    # Source = IOT because device_id was provided.
    assert ticket.source == "iot"


def test_one_sided_limit_min_only(app):
    ccp = CCPDefinition.query.filter_by(code="CCP-CORE-TEMP").first()
    assert ccp.critical_limit_max is None
    with app.test_request_context("/"):
        ok_meas = haccp_service.record_measurement(ccp_id=ccp.id, value=95.0)
        bad_meas = haccp_service.record_measurement(ccp_id=ccp.id, value=88.0)
        db.session.commit()
    assert ok_meas.is_within_limits is True
    assert bad_meas.is_within_limits is False


def test_inactive_ccp_rejected(app):
    ccp = _ccp()
    ccp.is_active = False
    db.session.commit()
    with app.test_request_context("/"), pytest.raises(haccp_service.HACCPError):
        haccp_service.record_measurement(ccp_id=ccp.id, value=200.0)


def test_audit_chain_records_measurements(app):
    ccp = _ccp()
    with app.test_request_context("/"):
        haccp_service.record_measurement(ccp_id=ccp.id, value=210.0)
        haccp_service.record_measurement(ccp_id=ccp.id, value=235.0)  # deviation
        db.session.commit()
        ok, broken = audit.verify_chain()
    assert ok and broken is None


def test_recent_measurements_ordering(app):
    ccp = _ccp()
    with app.test_request_context("/"):
        for v in (200.0, 205.0, 210.0):
            haccp_service.record_measurement(ccp_id=ccp.id, value=v)
        db.session.commit()
        recent = haccp_service.recent_measurements(ccp.id)
    assert len(recent) == 3
    # Newest first
    assert recent[0].measured_value == 210.0
