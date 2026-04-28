from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import db, utcnow


GENESIS_HASH = "0" * 64


class AuditLog(db.Model):
    """Append-only audit trail with chain-hashing for tamper evidence.

    Uses BIGINT auto-increment as PK so chain ordering is deterministic even
    when records share `occurred_at` to the microsecond. Production deployment
    also adds a PostgreSQL trigger blocking UPDATE/DELETE on this table.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    session_id: Mapped[str | None] = mapped_column(String(36))
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    diff: Mapped[dict | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    prev_checksum: Mapped[str] = mapped_column(String(64), nullable=False, default=GENESIS_HASH)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        Index("idx_audit_entity", "entity_type", "entity_id"),
        Index("idx_audit_user_occurred", "user_id", "occurred_at"),
    )

    def compute_checksum(self) -> str:
        payload = {
            "occurred_at": _to_utc_iso(self.occurred_at),
            "user_id": self.user_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "action": self.action,
            "diff": self.diff,
            "prev": self.prev_checksum,
        }
        serialized = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()


def _to_utc_iso(dt: datetime | None) -> str | None:
    """Stable UTC ISO string regardless of whether the DB returns naive datetimes.

    SQLite drops tzinfo on round-trip; PostgreSQL preserves it. Both must hash
    identically.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
