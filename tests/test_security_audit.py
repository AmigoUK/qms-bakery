"""OWASP self-audit regression tests.

One test per finding from `docs/security/owasp-self-audit.md`. Failing
any of these means a previously-fixed weakness has crept back in.
"""

from __future__ import annotations

import pytest

from app.blueprints.auth import _safe_next_url
from app.jobs.webhook import UnsafeWebhookURL, _validate_outbound_url


# ─── A10 SSRF: webhook URL guard ─────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6379/",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/router",
        "http://172.16.0.1/",
        "http://[::1]/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "http:///path-only",
    ],
)
def test_ssrf_guard_rejects_unsafe_url(url):
    with pytest.raises(UnsafeWebhookURL):
        _validate_outbound_url(url)


def test_ssrf_guard_allows_public_https(monkeypatch):
    """Public hostnames are allowed. We patch `getaddrinfo` so the test
    doesn't depend on real DNS."""
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    # Should not raise.
    _validate_outbound_url("https://example.com/hooks/incoming")


def test_ssrf_guard_can_be_bypassed_for_tests():
    """Unit tests that POST to localhost set allow_private=True. No
    production code path should ever pass this flag."""
    # No raise — the helper isn't called when allow_private=True.
    from app.jobs.webhook import _validate_outbound_url as v

    v("http://127.0.0.1:1/", allow_private=True)


# ─── A07 Open redirect: ?next= validation ────────────────────────────────


@pytest.mark.parametrize(
    "next_value",
    [
        "https://evil.example/",
        "//evil.example/qms",
        "http://evil.example/",
        "javascript:alert(1)",
        "ftp://internal/",
        "",
        None,
        "no-leading-slash",
    ],
)
def test_safe_next_url_rejects_external(app, next_value):
    """Anything with a scheme, netloc, or no leading slash falls back
    to the dashboard endpoint."""
    with app.test_request_context("/"):
        result = _safe_next_url(next_value)
    assert result.startswith("/")
    assert "evil.example" not in result
    assert "javascript" not in result.lower()


@pytest.mark.parametrize(
    "next_value", ["/tickets/", "/tickets/abc-123", "/admin/"]
)
def test_safe_next_url_allows_local_paths(app, next_value):
    with app.test_request_context("/"):
        assert _safe_next_url(next_value) == next_value


def test_login_redirect_strips_external_next(client):
    """End-to-end: hitting /auth/login?next=https://evil/ and submitting
    valid creds redirects to a local path, never the attacker host."""
    resp = client.post(
        "/auth/login?next=https://evil.example/qms",
        data={"email": "admin@test", "password": "Admin123!"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    location = resp.headers["Location"]
    assert "evil.example" not in location
    assert location.startswith("/") or location.startswith("http://localhost")


# ─── A02 Cookie hardening ─────────────────────────────────────────────────


def test_session_cookie_flags_present(app):
    """Default config (production) sets HttpOnly + SameSite=Lax + Secure
    on session and remember cookies. Tests can flip Secure off, but the
    other two must always hold."""
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["REMEMBER_COOKIE_HTTPONLY"] is True
    assert app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"


def test_session_cookie_secure_default_true(monkeypatch):
    """Boot a fresh app *without* the conftest test overrides to confirm
    the production default for SESSION_COOKIE_SECURE is True."""
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    from app import create_app

    fresh = create_app({"SECRET_KEY": "x", "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    assert fresh.config["SESSION_COOKIE_SECURE"] is True
    assert fresh.config["REMEMBER_COOKIE_SECURE"] is True


def test_session_cookie_secure_disabled_via_env(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    from app import create_app

    fresh = create_app({"SECRET_KEY": "x", "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    assert fresh.config["SESSION_COOKIE_SECURE"] is False
