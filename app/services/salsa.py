"""SALSA service - submit checklist responses, auto-ticket on nonconformities."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.extensions import db
from app.models._base import utcnow
from app.models.salsa import SalsaChecklist, SalsaResponse
from app.models.tickets import TicketCategory, TicketSeverity, TicketSource
from app.services import audit
from app.services import tickets as ticket_service


class SalsaError(Exception):
    pass


def list_checklists(active_only: bool = True) -> list[SalsaChecklist]:
    q = select(SalsaChecklist).order_by(SalsaChecklist.code)
    if active_only:
        q = q.where(SalsaChecklist.is_active.is_(True))
    return list(db.session.execute(q).scalars())


def submit_response(
    *,
    checklist_id: str,
    answers: dict[str, dict[str, Any]],
    user_id: str | None = None,
) -> SalsaResponse:
    """Persist a filled checklist; create one ticket if any item failed.

    `answers` shape: {"item_key": {"ok": True/False, "comment": "..."}}
    """
    checklist = db.session.get(SalsaChecklist, checklist_id)
    if checklist is None:
        raise SalsaError(f"Checklist {checklist_id} not found")
    if not checklist.is_active:
        raise SalsaError(f"Checklist {checklist.code} is inactive")

    valid_keys = {item["key"] for item in checklist.items}
    cleaned: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for key in valid_keys:
        entry = answers.get(key, {}) or {}
        ok = bool(entry.get("ok", False))
        cleaned[key] = {
            "ok": ok,
            "comment": (entry.get("comment") or "").strip()[:500] or None,
        }
        if not ok:
            failures.append(key)

    response = SalsaResponse(
        checklist_id=checklist.id,
        responded_by_user_id=user_id,
        responded_at=utcnow(),
        answers=cleaned,
        nonconformities_count=len(failures),
    )
    db.session.add(response)
    db.session.flush()

    audit.record(
        entity_type="salsa_response",
        entity_id=response.id,
        action="submit",
        diff={
            "checklist_code": checklist.code,
            "nonconformities": len(failures),
            "failed_items": failures,
        },
        user_id=user_id,
    )

    if failures and checklist.line_id:
        prompts = {item["key"]: item.get("prompt", {}) for item in checklist.items}
        first_fail = failures[0]
        # Use English prompt for ticket title (audit-friendly).
        title_en = (prompts.get(first_fail, {}) or {}).get("en", first_fail)
        title = f"SALSA nonconformity: {checklist.code} — {title_en}"
        ticket = ticket_service.create_ticket(
            line_id=checklist.line_id,
            title=title[:200],
            description=f"{len(failures)} failed item(s): {', '.join(failures)}",
            severity=TicketSeverity.HIGH,
            category=TicketCategory.HYGIENE,
            source=TicketSource.MANUAL,
            created_by_user_id=user_id,
            metadata={
                "salsa_checklist": checklist.code,
                "salsa_response_id": response.id,
                "failed_items": failures,
            },
        )
        response.linked_ticket_id = ticket.id
        db.session.flush()

    return response
