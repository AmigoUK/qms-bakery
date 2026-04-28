"""Trigger / responder models.

Trigger: a rule that fires when a metric satisfies a condition.
Responder: an action template (notify_in_app, create_ticket, escalate, ...).
TriggerResponder: M:N association with order_index.
TriggerExecution: audit-grade record of every fire + outcomes.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Table,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import TimestampMixin, UUIDPKMixin, db, utcnow

if TYPE_CHECKING:
    pass


class ResponderType(str, enum.Enum):
    NOTIFY_IN_APP = "notify_in_app"
    CREATE_TICKET = "create_ticket"
    ESCALATE = "escalate"
    WEBHOOK = "webhook"


class Trigger(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "triggers"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[dict] = mapped_column(JSON, nullable=False)  # {pl, en}
    scope: Mapped[str | None] = mapped_column(String(100))  # e.g. "line:LINE_A"
    # condition: {"metric":"temperature","operator":">","value":220,"duration_seconds":30}
    condition: Mapped[dict] = mapped_column(JSON, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    responders: Mapped[list["Responder"]] = relationship(
        secondary="trigger_responders", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<Trigger {self.code}>"


class Responder(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "responders"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[dict] = mapped_column(JSON, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


trigger_responders = Table(
    "trigger_responders",
    db.metadata,
    Column("trigger_id", String(36), ForeignKey("triggers.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "responder_id",
        String(36),
        ForeignKey("responders.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("order_index", Integer, nullable=False, default=0),
)


class TriggerExecution(db.Model):
    __tablename__ = "trigger_executions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    trigger_id: Mapped[str] = mapped_column(String(36), ForeignKey("triggers.id"))
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    payload: Mapped[dict | None] = mapped_column(JSON)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    responder_results: Mapped[dict | None] = mapped_column(JSON)
    linked_ticket_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tickets.id"))

    trigger: Mapped[Trigger] = relationship(lazy="joined")

    __table_args__ = (Index("idx_trigger_exec_at", "trigger_id", "fired_at"),)


class InAppNotification(db.Model):
    """Lightweight in-app notification, consumed by /notifications/feed."""

    __tablename__ = "in_app_notifications"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    role_code: Mapped[str | None] = mapped_column(String(32))  # broadcast to a role
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(String(2000))
    related_entity_type: Mapped[str | None] = mapped_column(String(64))
    related_entity_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
