"""SALSA checklist tests."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import SalsaChecklist, SalsaResponse, Ticket
from app.services import audit
from app.services import salsa as salsa_service


def _checklist(code: str = "HYG-DAILY") -> SalsaChecklist:
    return SalsaChecklist.query.filter_by(code=code).first()


def test_seed_creates_checklists(app):
    assert SalsaChecklist.query.count() >= 2
    cl = _checklist()
    assert len(cl.items) == 4
    keys = {it["key"] for it in cl.items}
    assert {"gloves", "hairnets", "no_jewellery", "health_check"} <= keys


def test_submit_clean_checklist_no_ticket(app):
    cl = _checklist()
    answers = {item["key"]: {"ok": True} for item in cl.items}
    with app.test_request_context("/"):
        resp = salsa_service.submit_response(checklist_id=cl.id, answers=answers)
        db.session.commit()
    assert resp.nonconformities_count == 0
    assert resp.linked_ticket_id is None
    assert Ticket.query.count() == 0


def test_submit_with_failure_creates_ticket(app):
    cl = _checklist()
    answers = {item["key"]: {"ok": True} for item in cl.items}
    answers["gloves"] = {"ok": False, "comment": "Two operators without gloves"}
    with app.test_request_context("/"):
        resp = salsa_service.submit_response(checklist_id=cl.id, answers=answers)
        db.session.commit()
    assert resp.nonconformities_count == 1
    assert resp.linked_ticket_id is not None
    ticket = db.session.get(Ticket, resp.linked_ticket_id)
    assert "SALSA" in ticket.title
    assert ticket.severity == "high"
    assert ticket.category == "hygiene"
    assert ticket.extra_data["salsa_checklist"] == cl.code
    assert "gloves" in ticket.extra_data["failed_items"]


def test_submit_multiple_failures_one_ticket(app):
    cl = _checklist()
    answers = {item["key"]: {"ok": False} for item in cl.items}
    with app.test_request_context("/"):
        resp = salsa_service.submit_response(checklist_id=cl.id, answers=answers)
        db.session.commit()
    assert resp.nonconformities_count == 4
    # Only one ticket per submission, even if many failures.
    assert Ticket.query.count() == 1
    ticket = Ticket.query.first()
    assert len(ticket.extra_data["failed_items"]) == 4


def test_unknown_keys_ignored(app):
    cl = _checklist()
    answers = {item["key"]: {"ok": True} for item in cl.items}
    answers["nonexistent_key"] = {"ok": False}
    with app.test_request_context("/"):
        resp = salsa_service.submit_response(checklist_id=cl.id, answers=answers)
        db.session.commit()
    assert "nonexistent_key" not in resp.answers
    assert resp.nonconformities_count == 0


def test_inactive_checklist_rejected(app):
    cl = _checklist()
    cl.is_active = False
    db.session.commit()
    with app.test_request_context("/"), pytest.raises(salsa_service.SalsaError):
        salsa_service.submit_response(checklist_id=cl.id, answers={})


def test_audit_chain_intact_after_salsa(app):
    cl = _checklist()
    answers = {item["key"]: {"ok": True} for item in cl.items}
    answers["gloves"] = {"ok": False}
    with app.test_request_context("/"):
        salsa_service.submit_response(checklist_id=cl.id, answers=answers)
        db.session.commit()
        ok, broken = audit.verify_chain()
    assert ok and broken is None
