"""Auth helpers - password hashing, RBAC decorators."""

from __future__ import annotations

from datetime import timedelta
from functools import wraps
from typing import Callable

import bcrypt
from flask import abort, current_app, flash, redirect, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models._base import utcnow
from app.models.auth import User
from app.services import audit


def hash_password(plain: str) -> str:
    rounds = current_app.config["BCRYPT_LOG_ROUNDS"]
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def authenticate(email: str, password: str) -> User | None:
    user = db.session.execute(
        db.select(User).where(User.email == email.lower().strip())
    ).unique().scalar_one_or_none()
    if not user or not user.is_active_flag:
        return None
    if user.is_locked():
        return None
    if not verify_password(password, user.password_hash):
        user.failed_attempts += 1
        threshold = current_app.config["LOCKOUT_THRESHOLD"]
        if user.failed_attempts >= threshold:
            user.locked_until = utcnow() + timedelta(
                minutes=current_app.config["LOCKOUT_MINUTES"]
            )
            audit.record(
                entity_type="user",
                entity_id=user.id,
                action="account_locked",
                diff={"failed_attempts": user.failed_attempts},
                user_id=user.id,
            )
        db.session.commit()
        return None

    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    audit.record(
        entity_type="user", entity_id=user.id, action="login_success", user_id=user.id
    )
    db.session.commit()
    return user


def require_permission(code: str) -> Callable:
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login", next=request.path))
            if not current_user.has_permission(code):
                audit.record(
                    entity_type="access",
                    action="denied",
                    diff={"required": code, "path": request.path},
                )
                db.session.commit()
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator
