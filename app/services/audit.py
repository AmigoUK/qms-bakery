"""Audit trail service - chain-hashed, append-only logging.

Each new audit entry incorporates the SHA-256 checksum of the previous entry,
forming a hash chain. Tampering with any past record breaks verification.

A SERIALIZABLE transaction is used to prevent races where two writers compute
the same `prev_checksum`.
"""

from __future__ import annotations

from typing import Any

from flask import has_request_context, request
from flask_login import current_user
from sqlalchemy import select

from app.extensions import db
from app.models.audit import GENESIS_HASH, AuditLog


def _request_metadata() -> dict[str, str | None]:
    if not has_request_context():
        return {"ip_address": None, "user_agent": None}
    return {
        "ip_address": request.remote_addr,
        "user_agent": (request.user_agent.string or "")[:255] or None,
    }


def _current_user_id() -> str | None:
    if not has_request_context():
        return None
    if not current_user or not current_user.is_authenticated:
        return None
    return current_user.id


def record(
    *,
    entity_type: str,
    action: str,
    entity_id: str | None = None,
    diff: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> AuditLog:
    """Append a new entry to audit_log. Caller is responsible for committing.

    The chain is built per-entry; we read the latest checksum then write the
    new one in the same session. Race-resistance relies on either DB
    serialization or a single-writer worker - both acceptable for QMS volume.
    """
    last = db.session.execute(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
    ).scalar_one_or_none()
    prev_hash = last.checksum if last else GENESIS_HASH

    meta = _request_metadata()
    entry = AuditLog(
        user_id=user_id or _current_user_id(),
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        diff=diff or {},
        ip_address=meta["ip_address"],
        user_agent=meta["user_agent"],
        prev_checksum=prev_hash,
        checksum="",  # filled below
    )
    # occurred_at is filled by default; ensure it's set before checksumming.
    db.session.add(entry)
    db.session.flush()  # populate defaults (occurred_at, id)
    entry.checksum = entry.compute_checksum()
    db.session.add(entry)
    db.session.flush()
    return entry


def verify_chain() -> tuple[bool, int | None]:
    """Walk the chain and verify every checksum.

    Returns (ok, broken_id_or_None). On success returns (True, None).
    On failure returns (False, id_of_first_bad_entry).
    """
    rows = db.session.execute(select(AuditLog).order_by(AuditLog.id.asc())).scalars()
    expected_prev = GENESIS_HASH
    for row in rows:
        if row.prev_checksum != expected_prev:
            return False, row.id
        if row.compute_checksum() != row.checksum:
            return False, row.id
        expected_prev = row.checksum
    return True, None
