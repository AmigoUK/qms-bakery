"""Ticket lifecycle tests."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import Ticket, TicketCategory, TicketSeverity, TicketSource, TicketStatus
from app.models.production import ProductionLine
from app.services import audit, tickets as ticket_service


def _line():
    return ProductionLine.query.filter_by(code="LINE_A").first()


def test_create_ticket_assigns_first_stage(app):
    with app.test_request_context("/"):
        ticket = ticket_service.create_ticket(
            line_id=_line().id,
            title="Oven 1 overheating",
            severity=TicketSeverity.HIGH,
            category=TicketCategory.TEMPERATURE_DEVIATION,
            source=TicketSource.IOT,
        )
        db.session.commit()

    assert ticket.ticket_number.startswith("QMS-")
    assert ticket.status == TicketStatus.NEW.value
    assert ticket.current_stage is not None
    assert ticket.current_stage.order_index == 0
    assert ticket.events[0].event_type == "created"


def test_ticket_number_is_monotonic(app):
    with app.test_request_context("/"):
        a = ticket_service.create_ticket(line_id=_line().id, title="A")
        b = ticket_service.create_ticket(line_id=_line().id, title="B")
        c = ticket_service.create_ticket(line_id=_line().id, title="C")
        db.session.commit()

    seq = lambda n: int(n.split("-")[-1])
    assert seq(b.ticket_number) == seq(a.ticket_number) + 1
    assert seq(c.ticket_number) == seq(b.ticket_number) + 1


def test_valid_state_transitions(app):
    with app.test_request_context("/"):
        ticket = ticket_service.create_ticket(line_id=_line().id, title="T")
        ticket_service.transition(ticket, TicketStatus.ASSIGNED, user_id=None)
        ticket_service.transition(ticket, TicketStatus.IN_PROGRESS, user_id=None)
        ticket_service.transition(ticket, TicketStatus.AWAITING_VERIFICATION, user_id=None)
        ticket_service.transition(ticket, TicketStatus.CLOSED, user_id=None)
        db.session.commit()

    assert ticket.status == TicketStatus.CLOSED.value
    assert ticket.closed_at is not None
    statuses = [e.to_status for e in ticket.events if e.event_type == "status_change"]
    assert statuses == ["assigned", "in_progress", "awaiting_verification", "closed"]


def test_invalid_transition_raises(app):
    with app.test_request_context("/"):
        ticket = ticket_service.create_ticket(line_id=_line().id, title="T")
        with pytest.raises(ticket_service.TicketError):
            ticket_service.transition(ticket, TicketStatus.CLOSED, user_id=None)


def test_audit_records_ticket_lifecycle(app):
    from app.models.audit import AuditLog

    with app.test_request_context("/"):
        ticket = ticket_service.create_ticket(line_id=_line().id, title="T")
        ticket_service.transition(ticket, TicketStatus.ASSIGNED, user_id=None)
        db.session.commit()

    actions = [a.action for a in AuditLog.query.filter_by(entity_id=ticket.id).all()]
    assert "create" in actions
    assert "status_change" in actions

    with app.test_request_context("/"):
        ok, broken = audit.verify_chain()
    assert ok and broken is None


def test_create_ticket_via_http(app, client, login_admin):
    login_admin()
    resp = client.post(
        "/tickets/new",
        data={
            "line_id": _line().id,
            "title": "Manual report from floor",
            "description": "Operator noticed visual defect on loaf",
            "severity": TicketSeverity.MEDIUM.value,
            "category": TicketCategory.OTHER.value,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), f"unexpected: {resp.status_code} {resp.data[:200]}"
    assert Ticket.query.count() == 1
