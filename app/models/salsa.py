"""SALSA (Safe And Local Supplier Approval) checklist models."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import TimestampMixin, UUIDPKMixin, db

if TYPE_CHECKING:
    from app.models.auth import User
    from app.models.production import ProductionLine
    from app.models.tickets import Ticket


class ChecklistFrequency(str, enum.Enum):
    DAILY = "daily"
    SHIFT = "shift"
    WEEKLY = "weekly"
    PER_EVENT = "per_event"
    MONTHLY = "monthly"


class SalsaChecklist(UUIDPKMixin, TimestampMixin, db.Model):
    """A reusable checklist template (e.g. 'Personnel hygiene — daily')."""

    __tablename__ = "salsa_checklists"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[dict] = mapped_column(JSON, nullable=False)  # {pl, en}
    frequency: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ChecklistFrequency.DAILY.value
    )
    line_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("production_lines.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # items: list of {key, prompt: {pl, en}, expected: bool|None}
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    line: Mapped["ProductionLine | None"] = relationship()
    responses: Mapped[list["SalsaResponse"]] = relationship(
        back_populates="checklist", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<SalsaChecklist {self.code}>"


class SalsaResponse(UUIDPKMixin, db.Model):
    """A single completed instance of a checklist."""

    __tablename__ = "salsa_responses"

    checklist_id: Mapped[str] = mapped_column(String(36), ForeignKey("salsa_checklists.id"))
    responded_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    responded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # answers: {item_key: {ok: bool, comment?: str}}
    answers: Mapped[dict] = mapped_column(JSON, nullable=False)
    nonconformities_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    linked_ticket_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tickets.id"))

    checklist: Mapped[SalsaChecklist] = relationship(back_populates="responses")
    responded_by: Mapped["User | None"] = relationship()
    linked_ticket: Mapped["Ticket | None"] = relationship()
