"""Compliance/admin TOTP enforcement gate.

When ENFORCE_TOTP_FOR_ROLES is on, an authenticated user whose role is
in ROLES_REQUIRING_2FA but who has not yet enrolled TOTP is bounced to
/auth/2fa/enroll on every request — except an allow-list of routes
(enrolment, logout, language switcher, healthchecks, static).

The test runs its own create_app rather than the conftest fixture
because the fixture deliberately leaves the gate off so existing tests
aren't redirected.
"""

from __future__ import annotations

import fakeredis
import pytest
from cryptography.fernet import Fernet

from app import create_app
from app.seeds import seed_initial
from app.services import totp as totp_service


@pytest.fixture()
def gated_app():
    server = fakeredis.FakeServer()
    redis_text = fakeredis.FakeRedis(server=server, decode_responses=True)
    redis_binary = fakeredis.FakeRedis(server=server)
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
            "BCRYPT_LOG_ROUNDS": 4,
            "AUTO_CREATE_TABLES": True,
            "REDIS_CLIENT": redis_text,
            "REDIS_BINARY_CLIENT": redis_binary,
            "RATELIMIT_ENABLED": False,
            "TOTP_ENC_KEY": Fernet.generate_key().decode("ascii"),
            "ENFORCE_TOTP_FOR_ROLES": True,  # the whole point
        }
    )
    with application.app_context():
        seed_initial(admin_email="admin@test", admin_password="Admin123!")
        yield application


def _login(client):
    return client.post(
        "/auth/login",
        data={"email": "admin@test", "password": "Admin123!"},
        follow_redirects=False,
    )


def test_admin_without_totp_is_bounced_to_enrolment(gated_app):
    client = gated_app.test_client()
    _login(client)
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/2fa/enroll" in resp.headers["Location"]


def test_enrolment_page_itself_is_reachable(gated_app):
    client = gated_app.test_client()
    _login(client)
    resp = client.get("/auth/2fa/enroll", follow_redirects=False)
    assert resp.status_code == 200


def test_logout_is_reachable_without_totp(gated_app):
    client = gated_app.test_client()
    _login(client)
    resp = client.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_language_switch_is_reachable_without_totp(gated_app):
    client = gated_app.test_client()
    _login(client)
    resp = client.get("/auth/lang/pl", follow_redirects=False)
    assert resp.status_code == 302
    # Should NOT be redirected back to /auth/2fa/enroll on the language route.
    assert "2fa" not in resp.headers["Location"]


def test_admin_with_totp_enrolled_is_not_bounced(gated_app):
    from app.extensions import db
    from app.models import User

    client = gated_app.test_client()
    with gated_app.app_context():
        user = User.query.filter_by(email="admin@test").first()
        secret, _uri = totp_service.begin_enrollment(user)
        # Verify a real code completes enrolment.
        import pyotp

        code = pyotp.TOTP(secret).now()
        assert totp_service.complete_enrollment(user, code) is True
        db.session.commit()
    _login(client)
    # First-factor login alone now redirects to /auth/login/2fa, NOT to enrolment.
    resp = client.post(
        "/auth/login",
        data={"email": "admin@test", "password": "Admin123!"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/auth/login/2fa" in resp.headers["Location"]


def test_operator_role_is_not_gated(gated_app):
    """Operators don't sign compliance docs; the gate doesn't apply."""
    from app.auth import hash_password
    from app.extensions import db
    from app.models import Role, User

    with gated_app.app_context():
        operator_role = Role.query.filter_by(code="operator").first()
        assert operator_role is not None
        op = User(
            email="op@test",
            password_hash=hash_password("Operator123!"),
            full_name="Floor Op",
            language="en",
            role_id=operator_role.id,
        )
        db.session.add(op)
        db.session.commit()
    client = gated_app.test_client()
    client.post(
        "/auth/login",
        data={"email": "op@test", "password": "Operator123!"},
        follow_redirects=False,
    )
    resp = client.get("/dashboard", follow_redirects=False)
    # Operator without TOTP isn't redirected to the enrolment page —
    # whatever their downstream permissions resolve to, the TOTP gate
    # doesn't trip for non-compliance roles.
    if resp.status_code == 302:
        assert "/auth/2fa/enroll" not in resp.headers["Location"]
