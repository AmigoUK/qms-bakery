"""Security headers on every response.

Hand-rolled `@app.after_request` hook (see app/security_headers.py).
Tests assert each header is present + sensible.
"""

from __future__ import annotations


def test_csp_present_on_html_response(client):
    resp = client.get("/healthz")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert csp, "CSP header missing"
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    # Inline scripts are forbidden — no 'unsafe-inline' under script-src
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp


def test_csp_script_src_is_self_only(client):
    """SortableJS is now self-hosted under /static/vendor/, so the CSP
    should not whitelist any external CDN. If a future template adds
    one, it must arrive with a corresponding CSP_EXTRA_SCRIPT_SRC entry
    rather than a silent CDN dependency."""
    resp = client.get("/healthz")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "cdn.jsdelivr.net" not in csp


def test_x_frame_options_deny(client):
    resp = client.get("/healthz")
    assert resp.headers.get("X-Frame-Options") == "DENY"


def test_x_content_type_options_nosniff(client):
    resp = client.get("/healthz")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_referrer_policy(client):
    resp = client.get("/healthz")
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


def test_permissions_policy_blocks_powerful_apis(client):
    resp = client.get("/healthz")
    pp = resp.headers.get("Permissions-Policy", "")
    assert "camera=()" in pp
    assert "geolocation=()" in pp


def test_hsts_only_on_https(client):
    """HSTS over plain HTTP is meaningless and confusing — RFC 6797 §7.2.
    The test client speaks HTTP, so HSTS should NOT appear."""
    resp = client.get("/healthz")
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_present_on_https(app):
    """Simulate `request.is_secure` via WSGI environ override."""
    with app.test_client() as client:
        resp = client.get(
            "/healthz",
            base_url="https://localhost",  # makes request.is_secure True
        )
        assert "Strict-Transport-Security" in resp.headers
        assert "max-age=31536000" in resp.headers["Strict-Transport-Security"]


def test_security_headers_can_be_disabled_via_config():
    """Lets the audit team A/B compare with vs. without the headers if
    they need to isolate a regression."""
    import fakeredis

    from app import create_app

    server = fakeredis.FakeServer()
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test",
            "BCRYPT_LOG_ROUNDS": 4,
            "AUTO_CREATE_TABLES": True,
            "REDIS_CLIENT": fakeredis.FakeRedis(server=server, decode_responses=True),
            "REDIS_BINARY_CLIENT": fakeredis.FakeRedis(server=server),
            "SECURITY_HEADERS_ENABLED": False,
        }
    )
    with app.test_client() as client:
        resp = client.get("/healthz")
        assert "Content-Security-Policy" not in resp.headers
        assert "X-Frame-Options" not in resp.headers
