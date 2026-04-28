"""HACCP measurement service - records measurements, auto-creates tickets on
out-of-spec readings, and writes everything to audit_log."""

from __future__ import annotations

from sqlalchemy import select

from app.extensions import db
from app.i18n import i18n_field
from app.models._base import utcnow
from app.models.haccp import CCPDefinition, CCPMeasurement
from app.models.tickets import TicketCategory, TicketSeverity, TicketSource
from app.services import audit
from app.services import tickets as ticket_service


class HACCPError(Exception):
    pass


def record_measurement(
    *,
    ccp_id: str,
    value: float,
    measured_by_user_id: str | None = None,
    device_id: str | None = None,
    notes: str | None = None,
) -> CCPMeasurement:
    ccp = db.session.get(CCPDefinition, ccp_id)
    if ccp is None:
        raise HACCPError(f"CCP {ccp_id} not found")
    if not ccp.is_active:
        raise HACCPError(f"CCP {ccp.code} is inactive")

    within = ccp.is_within_limits(value)
    measurement = CCPMeasurement(
        ccp_id=ccp.id,
        measured_value=float(value),
        measured_at=utcnow(),
        measured_by_user_id=measured_by_user_id,
        device_id=device_id,
        is_within_limits=within,
        notes=notes,
    )
    db.session.add(measurement)
    db.session.flush()

    audit.record(
        entity_type="ccp_measurement",
        entity_id=measurement.id,
        action="record",
        diff={
            "ccp_code": ccp.code,
            "value": float(value),
            "within_limits": within,
            "device_id": device_id,
        },
        user_id=measured_by_user_id,
    )

    if not within:
        title = (
            f"CCP deviation: {ccp.code} = {value} {ccp.unit} "
            f"(limits: {ccp.critical_limit_min}–{ccp.critical_limit_max})"
        )
        ticket = ticket_service.create_ticket(
            line_id=ccp.line_id,
            title=title[:200],
            description=notes,
            severity=TicketSeverity.CRITICAL,
            category=TicketCategory.TEMPERATURE_DEVIATION
            if ccp.parameter == "temperature"
            else TicketCategory.OTHER,
            source=TicketSource.IOT if device_id else TicketSource.MANUAL,
            created_by_user_id=measured_by_user_id,
            metadata={
                "ccp_id": ccp.id,
                "ccp_code": ccp.code,
                "measurement_id": measurement.id,
                "value": float(value),
                "limits": {"min": ccp.critical_limit_min, "max": ccp.critical_limit_max},
            },
        )
        ticket.is_ccp_related = True
        measurement.linked_ticket_id = ticket.id
        db.session.flush()

    return measurement


def list_definitions(line_id: str | None = None, active_only: bool = True) -> list[CCPDefinition]:
    q = select(CCPDefinition).order_by(CCPDefinition.code)
    if line_id:
        q = q.where(CCPDefinition.line_id == line_id)
    if active_only:
        q = q.where(CCPDefinition.is_active.is_(True))
    return list(db.session.execute(q).scalars())


def recent_measurements(ccp_id: str, limit: int = 50) -> list[CCPMeasurement]:
    q = (
        select(CCPMeasurement)
        .where(CCPMeasurement.ccp_id == ccp_id)
        .order_by(CCPMeasurement.measured_at.desc())
        .limit(limit)
    )
    return list(db.session.execute(q).scalars())
