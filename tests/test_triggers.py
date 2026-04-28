"""Trigger engine + API ingest tests."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.extensions import db
from app.models import (
    InAppNotification,
    Responder,
    ResponderType,
    Ticket,
    Trigger,
    TriggerExecution,
)
from app.models.production import ProductionLine
from app.services import audit
from app.services import triggers as trigger_service


def _line():
    return ProductionLine.query.filter_by(code="LINE_A").first()


def _trigger():
    return Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()


def test_seed_creates_trigger_with_responders(app):
    t = _trigger()
    assert t is not None
    assert t.condition == {"metric": "temperature", "operator": ">", "value": 220}
    assert len(t.responders) == 2
    types = {r.type for r in t.responders}
    assert ResponderType.NOTIFY_IN_APP.value in types
    assert ResponderType.CREATE_TICKET.value in types


def test_evaluate_condition_pure(app):
    cond = {"metric": "temperature", "operator": ">", "value": 220}
    assert trigger_service.evaluate_condition(cond, {"temperature": 230}) is True
    assert trigger_service.evaluate_condition(cond, {"temperature": 220}) is False
    assert trigger_service.evaluate_condition(cond, {"temperature": 200}) is False
    assert trigger_service.evaluate_condition(cond, {"other": 999}) is False
    assert trigger_service.evaluate_condition({"metric": "x", "operator": "??"}, {"x": 1}) is False


def test_below_threshold_does_not_fire(app):
    line = _line()
    with app.test_request_context("/"):
        fired = trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 200,
                "scope": f"line:{line.code}",
                "line_id": line.id,
            }
        )
        db.session.commit()
    assert fired == []
    assert TriggerExecution.query.count() == 0


def test_above_threshold_fires_and_creates_ticket(app):
    line = _line()
    with app.test_request_context("/"):
        fired = trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 232,
                "scope": f"line:{line.code}",
                "line_id": line.id,
                "source": "iot",
            }
        )
        db.session.commit()
    assert len(fired) == 1
    execution = fired[0]
    assert execution.success is True
    assert execution.linked_ticket_id is not None
    ticket = db.session.get(Ticket, execution.linked_ticket_id)
    assert ticket.severity == "high"
    assert ticket.source == "iot"
    assert ticket.extra_data["trigger_code"] == "OVEN1_OVERHEAT"
    # In-app notifications were broadcast to both QA and Line Manager roles.
    notifs = InAppNotification.query.all()
    assert {n.role_code for n in notifs} == {"qa", "line_manager"}


def test_scope_mismatch_skips_trigger(app):
    line = _line()
    with app.test_request_context("/"):
        # Trigger is scoped to LINE_A; payload says LINE_X.
        fired = trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 999,
                "scope": "line:LINE_X",
                "line_id": line.id,
            }
        )
        db.session.commit()
    assert fired == []


def test_dry_run_records_execution_but_no_side_effects(app):
    t = _trigger()
    t.dry_run = True
    db.session.commit()
    line = _line()
    with app.test_request_context("/"):
        fired = trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 999,
                "scope": f"line:{line.code}",
                "line_id": line.id,
            }
        )
        db.session.commit()
    assert fired == []  # `fire()` not called
    # But there's a logged execution and audit entry for the dry-run.
    exec_count = TriggerExecution.query.count()
    assert exec_count == 1
    assert Ticket.query.count() == 0
    assert InAppNotification.query.count() == 0


def test_audit_chain_intact_after_trigger(app):
    line = _line()
    with app.test_request_context("/"):
        trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 250,
                "scope": f"line:{line.code}",
                "line_id": line.id,
            }
        )
        db.session.commit()
        ok, broken = audit.verify_chain()
    assert ok and broken is None


def test_inactive_responder_skipped(app):
    t = _trigger()
    notify = next(r for r in t.responders if r.type == ResponderType.NOTIFY_IN_APP.value)
    notify.is_active = False
    db.session.commit()
    line = _line()
    with app.test_request_context("/"):
        fired = trigger_service.evaluate(
            {
                "metric": "temperature",
                "temperature": 240,
                "scope": f"line:{line.code}",
                "line_id": line.id,
            }
        )
        db.session.commit()
    assert len(fired) == 1
    # Notify was inactive so no notifications were created; ticket still was.
    assert InAppNotification.query.count() == 0
    assert Ticket.query.count() == 1


# ─── API ingest tests ────────────────────────────────────────────────────


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def api_client(app):
    app.config["API_KEYS"] = {"oven_gateway": "supersecret"}
    return app.test_client()


def test_api_health_requires_signature(api_client):
    resp = api_client.get("/api/v1/health")
    assert resp.status_code == 401


def test_api_health_with_signature(api_client):
    body = b""
    resp = api_client.get(
        "/api/v1/health",
        headers={
            "X-API-Key": "oven_gateway",
            "X-Signature": _sign("supersecret", body),
        },
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_api_measurement_invalid_key(api_client):
    body = json.dumps({"metric": "temperature", "temperature": 250}).encode()
    resp = api_client.post(
        "/api/v1/measurements",
        data=body,
        headers={"X-API-Key": "wrong", "X-Signature": "abc", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_api_measurement_fires_trigger(app, api_client):
    line = _line()
    body = json.dumps(
        {
            "metric": "temperature",
            "temperature": 235,
            "scope": f"line:{line.code}",
            "line_id": line.id,
            "source": "iot",
        }
    ).encode()
    resp = api_client.post(
        "/api/v1/measurements",
        data=body,
        headers={
            "X-API-Key": "oven_gateway",
            "X-Signature": _sign("supersecret", body),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert len(payload["triggers_fired"]) == 1
    assert payload["triggers_fired"][0]["trigger_code"] == "OVEN1_OVERHEAT"
    assert payload["triggers_fired"][0]["ticket_id"] is not None


def test_api_missing_metric_returns_400(api_client):
    body = json.dumps({"foo": "bar"}).encode()
    resp = api_client.post(
        "/api/v1/measurements",
        data=body,
        headers={
            "X-API-Key": "oven_gateway",
            "X-Signature": _sign("supersecret", body),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
