"""Magic-link token signing/verification tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from app.services import training_links


def _exp(secs_from_now: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=secs_from_now)


def test_round_trip(app):
    with app.app_context():
        token = training_links.issue_token("enrolment-abc", _exp(60))
        assert training_links.verify_token(token) == "enrolment-abc"


def test_token_shape(app):
    with app.app_context():
        token = training_links.issue_token("enrol-1", _exp(60))
        parts = token.split(".")
        assert len(parts) == 3
        # 3rd part is hex sha256 = 64 chars
        assert len(parts[2]) == 64
        assert all(c in "0123456789abcdef" for c in parts[2])


def test_expired_token_rejected(app):
    with app.app_context():
        token = training_links.issue_token("e1", _exp(1))
        time.sleep(1.1)
        with pytest.raises(training_links.InvalidToken, match="expired"):
            training_links.verify_token(token)


def test_tampered_signature_rejected(app):
    with app.app_context():
        token = training_links.issue_token("e1", _exp(60))
        eid, exp, sig = token.split(".")
        # Flip one hex character in the sig
        bad_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        with pytest.raises(training_links.InvalidToken, match="bad signature"):
            training_links.verify_token(f"{eid}.{exp}.{bad_sig}")


def test_tampered_enrolment_id_rejected(app):
    """An attacker cannot keep a valid sig and substitute another enrolment id."""
    with app.app_context():
        token = training_links.issue_token("e1", _exp(60))
        _, exp, sig = token.split(".")
        with pytest.raises(training_links.InvalidToken, match="bad signature"):
            training_links.verify_token(f"e2.{exp}.{sig}")


def test_malformed_token(app):
    with app.app_context():
        with pytest.raises(training_links.InvalidToken):
            training_links.verify_token("not-a-token")
        with pytest.raises(training_links.InvalidToken):
            training_links.verify_token("a.b")


def test_signing_key_rotation_invalidates_outstanding(app):
    with app.app_context():
        token = training_links.issue_token("e1", _exp(60))
        # Rotate the signing key.
        app.config["TRAINING_LINK_SIGNING_KEY"] = "freshly-rotated-key"
        with pytest.raises(training_links.InvalidToken, match="bad signature"):
            training_links.verify_token(token)


def test_naive_expires_at_rejected(app):
    with app.app_context():
        with pytest.raises(ValueError, match="timezone-aware"):
            training_links.issue_token("e1", datetime.now())  # naive
