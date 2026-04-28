from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import TimestampMixin, UUIDPKMixin, db

if TYPE_CHECKING:
    from app.models.auth import User
    from app.models.production import Pipeline, PipelineStage, ProductionLine


class TicketStatus(str, enum.Enum):
    NEW = "new"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    AWAITING_VERIFICATION = "awaiting_verification"
    ESCALATED = "escalated"
    REJECTED = "rejected"
    CLOSED = "closed"


class TicketSeverity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TicketSource(str, enum.Enum):
    MANUAL = "manual"
    IOT = "iot"
    API = "api"


class TicketCategory(str, enum.Enum):
    TEMPERATURE_DEVIATION = "temperature_deviation"
    WEIGHT_OUT_OF_SPEC = "weight_out_of_spec"
    FOREIGN_BODY = "foreign_body"
    ALLERGEN_CROSS_CONTACT = "allergen_cross_contact"
    HYGIENE = "hygiene"
    OTHER = "other"


# Allowed state transitions. Used by the service layer to validate moves.
TICKET_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.NEW: {TicketStatus.ASSIGNED, TicketStatus.REJECTED},
    TicketStatus.ASSIGNED: {TicketStatus.IN_PROGRESS, TicketStatus.ESCALATED, TicketStatus.REJECTED},
    TicketStatus.IN_PROGRESS: {
        TicketStatus.AWAITING_VERIFICATION,
        TicketStatus.ESCALATED,
        TicketStatus.REJECTED,
    },
    TicketStatus.AWAITING_VERIFICATION: {TicketStatus.CLOSED, TicketStatus.IN_PROGRESS},
    TicketStatus.ESCALATED: {TicketStatus.IN_PROGRESS, TicketStatus.CLOSED, TicketStatus.REJECTED},
    TicketStatus.REJECTED: set(),
    TicketStatus.CLOSED: set(),
}


CLOSED_STATES: set[TicketStatus] = {TicketStatus.CLOSED, TicketStatus.REJECTED}


class Ticket(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "tickets"

    ticket_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    line_id: Mapped[str] = mapped_column(String(36), ForeignKey("production_lines.id"))
    pipeline_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("pipelines.id"))
    current_stage_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pipeline_stages.id")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=TicketStatus.NEW.value)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=TicketSource.MANUAL.value)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TicketSeverity.MEDIUM.value
    )
    category: Mapped[str] = mapped_column(
        String(64), nullable=False, default=TicketCategory.OTHER.value
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    description_lang: Mapped[str | None] = mapped_column(String(2))
    is_ccp_related: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    extra_data: Mapped[dict | None] = mapped_column("metadata", JSON)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    assigned_to_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    line: Mapped["ProductionLine"] = relationship()
    pipeline: Mapped["Pipeline | None"] = relationship()
    current_stage: Mapped["PipelineStage | None"] = relationship()
    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_user_id])
    assignee: Mapped["User | None"] = relationship(foreign_keys=[assigned_to_user_id])
    events: Mapped[list["TicketEvent"]] = relationship(
        back_populates="ticket",
        order_by="TicketEvent.occurred_at.asc()",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_tickets_status_created", "status", "created_at"),
        Index("idx_tickets_line_status", "line_id", "status"),
        Index("idx_tickets_severity_created", "severity", "created_at"),
    )

    @property
    def is_open(self) -> bool:
        return TicketStatus(self.status) not in CLOSED_STATES

    def __repr__(self) -> str:
        return f"<Ticket {self.ticket_number} {self.status}>"


class TicketEvent(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "ticket_events"

    ticket_id: Mapped[str] = mapped_column(String(36), ForeignKey("tickets.id"))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    comment: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    payload: Mapped[dict | None] = mapped_column(JSON)

    ticket: Mapped[Ticket] = relationship(back_populates="events")
    user: Mapped["User | None"] = relationship()

    __table_args__ = (Index("idx_events_ticket_occurred", "ticket_id", "occurred_at"),)
