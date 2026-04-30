"""HMAC-SHA256 magic-link tokens for the training flow.

Token shape: `{enrolment_id}.{exp_unix}.{sig_hex}` — three dot-separated
parts. Signature pattern mirrors `app/jobs/webhook.py:125` and the
verification mirrors `app/blueprints/api.py:35`. Both expectations are
checked at decode time:

  1. The signature is a valid HMAC over `{enrolment_id}:{exp_unix}` with
     the configured signing key.
  2. The current time is before `exp_unix`.
  3. (Defence in depth, in the caller) the database's stored
     `Enrolment.expires_at` is also still in the future, so revocation
     is possible by editing the row.

Signing key precedence: app.config["TRAINING_LINK_SIGNING_KEY"], with a
fallback to SECRET_KEY for dev. Rotating the key invalidates every
outstanding link — desirable; documented in the README.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from flask import current_app


class InvalidToken(ValueError):
    """Token failed verification — bad shape, expired, or bad signature."""


def _signing_key() -> bytes:
    cfg = current_app.config
    key = cfg.get("TRAINING_LINK_SIGNING_KEY") or cfg.get("SECRET_KEY")
    if not key:
        raise RuntimeError(
            "TRAINING_LINK_SIGNING_KEY (or SECRET_KEY) must be set "
            "to issue training magic links"
        )
    return key.encode("utf-8") if isinstance(key, str) else key


def _body(enrolment_id: str, exp_unix: int) -> bytes:
    return f"{enrolment_id}:{exp_unix}".encode("utf-8")


def issue_token(enrolment_id: str, expires_at: datetime) -> str:
    """Build a signed magic-link token. `expires_at` must be timezone-aware."""
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    exp_unix = int(expires_at.timestamp())
    sig = hmac.new(_signing_key(), _body(enrolment_id, exp_unix), hashlib.sha256).hexdigest()
    return f"{enrolment_id}.{exp_unix}.{sig}"


def verify_token(token: str) -> str:
    """Verify the token shape, signature, and embedded TTL.

    Returns the enrolment_id on success; raises InvalidToken otherwise.
    The caller is expected to additionally check the row's stored
    `expires_at` against the current time — that's the cheap revocation
    handle.
    """
    if not token or token.count(".") != 2:
        raise InvalidToken("malformed token")
    enrolment_id, exp_str, sig = token.split(".")
    try:
        exp_unix = int(exp_str)
    except ValueError as exc:
        raise InvalidToken("bad expiry") from exc
    expected = hmac.new(_signing_key(), _body(enrolment_id, exp_unix), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise InvalidToken("bad signature")
    if datetime.now(timezone.utc).timestamp() >= exp_unix:
        raise InvalidToken("expired")
    return enrolment_id
