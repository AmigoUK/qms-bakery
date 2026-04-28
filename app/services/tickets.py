"""Ticket service - lifecycle, transitions, audit integration."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from app.extensions import db
from app.models._base import utcnow
from app.models.production import Pipeline, PipelineStage, ProductionLine
from app.models.tickets import (
    CLOSED_STATES,
    TICKET_TRANSITIONS,
    Ticket,
    TicketCategory,
    TicketEvent,
    TicketSeverity,
    TicketSource,
    TicketStatus,
)
from app.services import audit


class TicketError(Exception):
    """Domain error from the ticket service."""


def generate_ticket_number() -> str:
    """Format: QMS-YYYY-NNNNN, monotonic per year."""
    year = date.today().year
    prefix = f"QMS-{year}-"
    last = db.session.execute(
        select(Ticket.ticket_number)
        .where(Ticket.ticket_number.like(f"{prefix}%"))
        .order_by(Ticket.ticket_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    next_seq = 1 if last is None else int(last.split("-")[-1]) + 1
    return f"{prefix}{next_seq:05d}"


def create_ticket(
    *,
    line_id: str,
    title: str,
    description: str | None = None,
    severity: TicketSeverity = TicketSeverity.MEDIUM,
    category: TicketCategory = TicketCategory.OTHER,
    source: TicketSource = TicketSource.MANUAL,
    created_by_user_id: str | None = None,
    description_lang: str | None = None,
    metadata: dict | None = None,
) -> Ticket:
    if not (line := db.session.get(ProductionLine, line_id)):
        raise TicketError(f"Production line {line_id} not found")
    if not line.is_active:
        raise TicketError(f"Production line {line.code} is inactive")

    pipeline = db.session.execute(
        select(Pipeline)
        .where(Pipeline.line_id == line_id, Pipeline.is_active.is_(True))
        .order_by(Pipeline.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    first_stage: PipelineStage | None = None
    if pipeline and pipeline.stages:
        first_stage = pipeline.stages[0]

    ticket = Ticket(
        ticket_number=generate_ticket_number(),
        line_id=line_id,
        pipeline_id=pipeline.id if pipeline else None,
        current_stage_id=first_stage.id if first_stage else None,
        status=TicketStatus.NEW.value,
        source=source.value,
        severity=severity.value,
        category=category.value,
        title=title.strip()[:200],
        description=(description or "").strip() or None,
        description_lang=description_lang,
        created_by_user_id=created_by_user_id,
        extra_data=metadata or {},
    )
    db.session.add(ticket)
    db.session.flush()

    db.session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type="created",
            from_status=None,
            to_status=ticket.status,
            user_id=created_by_user_id,
            occurred_at=utcnow(),
            payload={"source": source.value, "severity": severity.value},
        )
    )
    audit.record(
        entity_type="ticket",
        entity_id=ticket.id,
        action="create",
        diff={
            "ticket_number": ticket.ticket_number,
            "title": ticket.title,
            "severity": ticket.severity,
            "source": ticket.source,
        },
        user_id=created_by_user_id,
    )
    db.session.flush()
    return ticket


def transition(
    ticket: Ticket, new_status: TicketStatus, *, user_id: str | None, comment: str | None = None
) -> Ticket:
    current = TicketStatus(ticket.status)
    allowed = TICKET_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise TicketError(
            f"Invalid transition: {current.value} -> {new_status.value}"
        )

    if new_status == TicketStatus.ASSIGNED and not ticket.assigned_to_user_id:
        ticket.assigned_to_user_id = user_id

    if new_status in CLOSED_STATES:
        ticket.closed_at = utcnow()

    ticket.status = new_status.value
    ticket.version += 1

    db.session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type="status_change",
            from_status=current.value,
            to_status=new_status.value,
            user_id=user_id,
            occurred_at=utcnow(),
            comment=comment,
        )
    )
    audit.record(
        entity_type="ticket",
        entity_id=ticket.id,
        action="status_change",
        diff={"from": current.value, "to": new_status.value, "comment": comment},
        user_id=user_id,
    )
    db.session.flush()
    return ticket


def assign_to(ticket: Ticket, user_id: str, *, by_user_id: str | None = None) -> Ticket:
    prev_assignee = ticket.assigned_to_user_id
    ticket.assigned_to_user_id = user_id
    if ticket.status == TicketStatus.NEW.value:
        return transition(ticket, TicketStatus.ASSIGNED, user_id=by_user_id or user_id)
    db.session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type="assigned",
            user_id=by_user_id,
            occurred_at=utcnow(),
            payload={"from": prev_assignee, "to": user_id},
        )
    )
    audit.record(
        entity_type="ticket",
        entity_id=ticket.id,
        action="assigned",
        diff={"from": prev_assignee, "to": user_id},
        user_id=by_user_id,
    )
    db.session.flush()
    return ticket


def add_comment(ticket: Ticket, *, user_id: str, comment: str) -> TicketEvent:
    event = TicketEvent(
        ticket_id=ticket.id,
        event_type="comment",
        user_id=user_id,
        occurred_at=utcnow(),
        comment=comment.strip()[:5000],
    )
    db.session.add(event)
    audit.record(
        entity_type="ticket",
        entity_id=ticket.id,
        action="comment",
        diff={"length": len(comment)},
        user_id=user_id,
    )
    db.session.flush()
    return event


def list_tickets(
    *,
    status: str | None = None,
    line_id: str | None = None,
    severity: str | None = None,
    open_only: bool = False,
    limit: int = 100,
) -> list[Ticket]:
    query = select(Ticket).order_by(Ticket.created_at.desc())
    if status:
        query = query.where(Ticket.status == status)
    if line_id:
        query = query.where(Ticket.line_id == line_id)
    if severity:
        query = query.where(Ticket.severity == severity)
    if open_only:
        query = query.where(Ticket.status.notin_([s.value for s in CLOSED_STATES]))
    query = query.limit(limit)
    return list(db.session.execute(query).scalars())


def stats_overview() -> dict:
    open_total = db.session.execute(
        select(func.count(Ticket.id)).where(
            Ticket.status.notin_([s.value for s in CLOSED_STATES])
        )
    ).scalar_one()
    high_severity = db.session.execute(
        select(func.count(Ticket.id)).where(
            Ticket.severity.in_([TicketSeverity.HIGH.value, TicketSeverity.CRITICAL.value]),
            Ticket.status.notin_([s.value for s in CLOSED_STATES]),
        )
    ).scalar_one()
    closed_today = db.session.execute(
        select(func.count(Ticket.id)).where(
            Ticket.status == TicketStatus.CLOSED.value,
            func.date(Ticket.closed_at) == date.today(),
        )
    ).scalar_one()
    lines = db.session.execute(
        select(func.count(ProductionLine.id)).where(ProductionLine.is_active.is_(True))
    ).scalar_one()
    return {
        "open_tickets": open_total,
        "high_severity": high_severity,
        "closed_today": closed_today,
        "production_lines": lines,
    }
