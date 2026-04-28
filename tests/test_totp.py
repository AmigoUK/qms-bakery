"""TOTP 2FA tests."""

from __future__ import annotations

import pyotp

from app.extensions import db
from app.models import User
from app.services import totp as totp_service


def _admin():
    return User.query.filter_by(email="admin@test").first()


def test_role_requires_totp():
    assert totp_service.role_requires_totp("admin") is True
    assert totp_service.role_requires_totp("compliance") is True
    assert totp_service.role_requires_totp("operator") is False
    assert totp_service.role_requires_totp(None) is False


def test_begin_and_complete_enrollment(app):
    with app.app_context():
        user = _admin()
        secret, uri = totp_service.begin_enrollment(user)
        db.session.commit()
        assert user.totp_secret == secret
        assert user.totp_enrolled_at is None
        assert "otpauth://totp/" in uri
        assert "QMS-Bakery" in uri

        # Wrong code rejected.
        assert totp_service.complete_enrollment(user, "000000") is False
        assert user.totp_enrolled_at is None

        # Real code from same secret accepted.
        live_code = pyotp.TOTP(secret).now()
        assert totp_service.complete_enrollment(user, live_code) is True
        assert user.totp_enrolled_at is not None
        assert user.totp_enabled is True


def test_verify_code_rejects_when_not_enrolled(app):
    user = _admin()
    assert user.totp_enabled is False
    assert totp_service.verify_code(user, "123456") is False


def test_verify_code_format_validation(app):
    with app.app_context():
        user = _admin()
        totp_service.begin_enrollment(user)
        user.totp_enrolled_at = db.session.query(User).first().created_at  # any non-null
        assert totp_service.verify_code(user, "abc123") is False
        assert totp_service.verify_code(user, "12345") is False  # too short
        assert totp_service.verify_code(user, "1234567890") is False  # too long


def test_login_with_totp_requires_second_step(app, client):
    # Enroll admin.
    with app.app_context():
        user = _admin()
        secret, _uri = totp_service.begin_enrollment(user)
        live = pyotp.TOTP(secret).now()
        totp_service.complete_enrollment(user, live)
        db.session.commit()
        secret_for_test = user.totp_secret

    # First step: email + password — should redirect to /auth/login/2fa.
    resp = client.post(
        "/auth/login",
        data={"email": "admin@test", "password": "Admin123!"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "/login/2fa" in resp.location

    # Going to dashboard before 2FA still bounces to login.
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302)

    # Submit a wrong code.
    bad = client.post(
        "/auth/login/2fa", data={"code": "000000"}, follow_redirects=False
    )
    assert bad.status_code == 200  # form re-rendered with error

    # Submit a valid code -> session is upgraded.
    good_code = pyotp.TOTP(secret_for_test).now()
    good = client.post(
        "/auth/login/2fa", data={"code": good_code}, follow_redirects=False
    )
    assert good.status_code in (302, 303)
    # Now dashboard should be reachable.
    resp = client.get("/")
    assert resp.status_code == 200


def test_2fa_endpoint_without_pending_session_redirects(client):
    resp = client.get("/auth/login/2fa", follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert "/auth/login" in resp.location


def test_enroll_route_renders_secret(app, client, login_admin):
    login_admin()
    resp = client.get("/auth/2fa/enroll")
    assert resp.status_code == 200
    # Page should contain an otpauth URI.
    assert b"otpauth://totp/" in resp.data
