from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from flask_login import UserMixin
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import TimestampMixin, UUIDPKMixin, db

if TYPE_CHECKING:
    from app.models.tickets import Ticket


class UserRoleEnum(str, enum.Enum):
    OPERATOR = "operator"
    QA = "qa"
    LINE_MANAGER = "line_manager"
    COMPLIANCE = "compliance"
    PLANT_MANAGER = "plant_manager"
    ADMIN = "admin"


role_permissions = Table(
    "role_permissions",
    db.metadata,
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_id",
        String(36),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Permission(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:
        return f"<Permission {self.code}>"


class Role(UUIDPKMixin, TimestampMixin, db.Model):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name_pl: Mapped[str] = mapped_column(String(64), nullable=False)
    name_en: Mapped[str] = mapped_column(String(64), nullable=False)

    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions, lazy="joined"
    )

    def has_permission(self, code: str) -> bool:
        return any(p.code == code for p in self.permissions)

    def __repr__(self) -> str:
        return f"<Role {self.code}>"


class User(UUIDPKMixin, TimestampMixin, UserMixin, db.Model):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    language: Mapped[str] = mapped_column(String(2), nullable=False, default="en")
    is_active_flag: Mapped[bool] = mapped_column(
        "is_active", Boolean, nullable=False, default=True
    )
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role_id: Mapped[str] = mapped_column(String(36), ForeignKey("roles.id"), nullable=False)

    role: Mapped[Role] = relationship(lazy="joined")

    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.is_active_flag

    def has_permission(self, code: str) -> bool:
        return self.role is not None and self.role.has_permission(code)

    def is_locked(self) -> bool:
        from datetime import timezone

        from app.models._base import utcnow

        if self.locked_until is None:
            return False
        locked = self.locked_until
        if locked.tzinfo is None:
            locked = locked.replace(tzinfo=timezone.utc)
        return locked > utcnow()

    def __repr__(self) -> str:
        return f"<User {self.email}>"
