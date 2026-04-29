"""Trigger evaluation + responder dispatch.

Synchronous, in-process implementation. Designed to be wrapped by an RQ
worker later: `evaluate(payload)` is pure with respect to side-effects via
the SQLAlchemy session, so the same function runs identically when called
from a request handler, an MQTT bridge, or a queued task.

Conditions (JSONB shape):
    { "metric": "temperature", "operator": ">", "value": 220 }
    { "metric": "temperature", "operator": ">", "value": 220, "duration_seconds": 30 }
    Operators: ==, !=, <, <=, >, >=
    `duration_seconds` (optional, integer): when > 0, the trigger only
    fires after the condition has been continuously True for that many
    seconds across consecutive evaluations of the same scope. State is
    held in Redis (`trigger_state:<id>:<scope>:first_true`); see
    `app/services/trigger_state.py`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.extensions import db
from app.models._base import utcnow
from app.models.tickets import TicketSeverity, TicketSource
from app.models.triggers import (
    InAppNotification,
    Responder,
    ResponderType,
    Trigger,
    TriggerExecution,
)
from app.services import audit
from app.services import tickets as ticket_service


_OPERATORS: dict[str, Any] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


class TriggerError(Exception):
    pass


def _scope_matches(trigger_scope: str | None, payload_scope: str | None) -> bool:
    if not trigger_scope:
        return True
    return trigger_scope == payload_scope


def evaluate_condition(condition: dict, payload: dict) -> bool:
    metric = condition.get("metric")
    op = condition.get("operator")
    threshold = condition.get("value")
    if metric is None or op not in _OPERATORS:
        return False
    if metric not in payload:
        return False
    try:
        return bool(_OPERATORS[op](payload[metric], threshold))
    except TypeError:
        return False


def fire(trigger: Trigger, payload: dict) -> TriggerExecution:
    """Execute every responder attached to `trigger`. Returns the execution row."""
    execution = TriggerExecution(
        trigger_id=trigger.id,
        fired_at=utcnow(),
        payload=payload,
        responder_results={},
    )
    db.session.add(execution)
    db.session.flush()

    results: dict[str, dict] = {}
    overall_ok = True
    linked_ticket_id: str | None = None

    for responder in trigger.responders:
        if not responder.is_active:
            continue
        try:
            outcome = _dispatch_responder(responder, trigger, payload)
            results[responder.code] = {"ok": True, **outcome}
            if outcome.get("ticket_id") and not linked_ticket_id:
                linked_ticket_id = outcome["ticket_id"]
        except Exception as exc:  # responder failures must not abort the chain
            overall_ok = False
            results[responder.code] = {"ok": False, "error": str(exc)}

    execution.responder_results = results
    execution.success = overall_ok
    execution.linked_ticket_id = linked_ticket_id

    audit.record(
        entity_type="trigger",
        entity_id=trigger.id,
        action="fire",
        diff={
            "trigger_code": trigger.code,
            "payload": payload,
            "responder_results": results,
            "dry_run": trigger.dry_run,
        },
    )
    db.session.flush()
    return execution


def evaluate(payload: dict) -> list[TriggerExecution]:
    """Evaluate every active trigger against `payload`. Fire matching ones.

    `payload` minimally contains `{"metric": "...", "<metric_key>": value, "scope": "..."}`.
    Scope (e.g. "line:LINE_A") narrows triggers; omit to evaluate globally.
    """
    triggers = db.session.execute(
        select(Trigger).where(Trigger.is_active.is_(True))
    ).unique().scalars().all()

    fired: list[TriggerExecution] = []
    payload_scope = payload.get("scope")
    for trigger in triggers:
        if not _scope_matches(trigger.scope, payload_scope):
            continue
        condition_true = evaluate_condition(trigger.condition, payload)
        duration = int(trigger.condition.get("duration_seconds") or 0)
        if duration > 0:
            from app.services import trigger_state

            if not condition_true:
                trigger_state.reset_duration_state(trigger.id, payload_scope)
                continue
            if not trigger_state.should_fire_with_duration(
                trigger.id, payload_scope, duration
            ):
                continue
        elif not condition_true:
            continue
        if trigger.dry_run:
            db.session.add(
                TriggerExecution(
                    trigger_id=trigger.id,
                    fired_at=utcnow(),
                    payload={**payload, "_dry_run": True},
                    responder_results={"dry_run": True},
                    success=True,
                )
            )
            audit.record(
                entity_type="trigger",
                entity_id=trigger.id,
                action="fire_dry_run",
                diff={"payload": payload},
            )
            db.session.flush()
            continue
        fired.append(fire(trigger, payload))
    return fired


def _dispatch_responder(responder: Responder, trigger: Trigger, payload: dict) -> dict:
    rtype = ResponderType(responder.type)
    cfg = responder.config or {}

    if rtype is ResponderType.NOTIFY_IN_APP:
        recipients = cfg.get("recipients") or []  # list of {user_id} or {role_code}
        title = _interpolate(cfg.get("title", "Trigger fired"), payload, trigger)
        body = _interpolate(cfg.get("body", ""), payload, trigger)
        count = 0
        for r in recipients:
            db.session.add(
                InAppNotification(
                    user_id=r.get("user_id"),
                    role_code=r.get("role_code"),
                    severity=trigger.severity,
                    title=title[:200],
                    body=body[:2000] if body else None,
                    related_entity_type="trigger",
                    related_entity_id=trigger.id,
                )
            )
            count += 1
        db.session.flush()
        return {"notifications_created": count}

    if rtype is ResponderType.CREATE_TICKET:
        line_id = cfg.get("line_id") or payload.get("line_id")
        if not line_id:
            raise TriggerError("create_ticket responder requires line_id")
        title = _interpolate(
            cfg.get("title", f"Auto: {trigger.code}"), payload, trigger
        )
        ticket = ticket_service.create_ticket(
            line_id=line_id,
            title=title[:200],
            description=_interpolate(cfg.get("description", ""), payload, trigger),
            severity=TicketSeverity(trigger.severity),
            source=TicketSource.IOT if payload.get("source") == "iot" else TicketSource.API,
            metadata={"trigger_code": trigger.code, "payload": payload},
        )
        return {"ticket_id": ticket.id, "ticket_number": ticket.ticket_number}

    if rtype is ResponderType.ESCALATE:
        ticket_id = payload.get("ticket_id") or cfg.get("ticket_id")
        if not ticket_id:
            raise TriggerError("escalate responder requires ticket_id in payload or config")
        from app.models import Ticket, TicketStatus

        ticket = db.session.get(Ticket, ticket_id)
        if ticket is None:
            raise TriggerError(f"Ticket {ticket_id} not found")
        ticket_service.transition(
            ticket, TicketStatus.ESCALATED, user_id=None, comment="Auto-escalated by trigger"
        )
        return {"escalated_ticket_id": ticket_id}

    if rtype is ResponderType.WEBHOOK:
        # In-process placeholder: real implementation enqueues HTTP POST in RQ.
        return {"queued_webhook": cfg.get("url")}

    raise TriggerError(f"Unknown responder type: {responder.type}")


def _interpolate(template: str, payload: dict, trigger: Trigger) -> str:
    """Cheap {{ key }} substitution; falls back to raw template on KeyError."""
    if not template:
        return ""
    try:
        ctx = {**payload, "trigger_code": trigger.code, "severity": trigger.severity}
        return template.format(**ctx)
    except (KeyError, IndexError, ValueError):
        return template
