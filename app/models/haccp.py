"""HACCP / Critical Control Points models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
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


class CCPDefinition(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "ccp_definitions"

    line_id: Mapped[str] = mapped_column(String(36), ForeignKey("production_lines.id"))
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[dict] = mapped_column(JSON, nullable=False)  # {pl, en}
    parameter: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "temperature"
    unit: Mapped[str] = mapped_column(String(16), nullable=False)  # e.g. "°C"
    critical_limit_min: Mapped[float | None] = mapped_column(Float)
    critical_limit_max: Mapped[float | None] = mapped_column(Float)
    monitoring_frequency_minutes: Mapped[int | None] = mapped_column(Integer)
    corrective_action: Mapped[dict | None] = mapped_column(JSON)  # {pl, en} template
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    line: Mapped["ProductionLine"] = relationship()
    measurements: Mapped[list["CCPMeasurement"]] = relationship(
        back_populates="ccp", cascade="all, delete-orphan"
    )

    def is_within_limits(self, value: float) -> bool:
        if self.critical_limit_min is not None and value < self.critical_limit_min:
            return False
        if self.critical_limit_max is not None and value > self.critical_limit_max:
            return False
        return True

    def __repr__(self) -> str:
        return f"<CCPDefinition {self.code} {self.parameter}>"


class CCPMeasurement(UUIDPKMixin, db.Model):
    __tablename__ = "ccp_measurements"

    ccp_id: Mapped[str] = mapped_column(String(36), ForeignKey("ccp_definitions.id"))
    measured_value: Mapped[float] = mapped_column(Float, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    measured_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    device_id: Mapped[str | None] = mapped_column(String(64))
    is_within_limits: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500))
    linked_ticket_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tickets.id"))
    extra_data: Mapped[dict | None] = mapped_column("metadata", JSON)

    ccp: Mapped[CCPDefinition] = relationship(back_populates="measurements")
    measured_by: Mapped["User | None"] = relationship()
    linked_ticket: Mapped["Ticket | None"] = relationship()

    __table_args__ = (Index("idx_ccp_meas_ccp_at", "ccp_id", "measured_at"),)
