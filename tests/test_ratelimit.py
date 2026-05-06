"""Per-IP rate limiting on /auth/login + /api/v1/measurements.

Default-off in tests (see conftest); each test re-enables explicitly so
the rate-limit counters live in the per-test fakeredis.
"""

from __future__ import annotations

import hashlib
import hmac


def _enable_ratelimit(app, *, api_max: int = 600):
    app.config["RATELIMIT_ENABLED"] = True
    app.config["RATELIMIT_API_MAX"] = api_max


def test_login_allows_under_limit(app, client):
    _enable_ratelimit(app)
    for _ in range(5):
        resp = client.get("/auth/login")
        assert resp.status_code == 200


def test_login_blocks_after_limit(app, client):
    _enable_ratelimit(app)
    # 10/min limit. 11th request must come back 429.
    for i in range(10):
        resp = client.get("/auth/login")
        assert resp.status_code == 200, f"request {i} unexpectedly limited"
    blocked = client.get("/auth/login")
    assert blocked.status_code == 429
    body = blocked.get_json()
    assert body["error"] == "rate_limited"
    assert blocked.headers.get("Retry-After")


def test_login_remaining_header_decrements(app, client):
    _enable_ratelimit(app)
    r1 = client.get("/auth/login")
    r2 = client.get("/auth/login")
    rem1 = int(r1.headers.get("X-RateLimit-Remaining", "0"))
    rem2 = int(r2.headers.get("X-RateLimit-Remaining", "0"))
    assert rem1 == 9
    assert rem2 == 8


def test_api_blocks_under_floods(app, client):
    """/api/v1/measurements should rate-limit BEFORE signature check, so
    a flood of bad-key requests gets throttled."""
    _enable_ratelimit(app, api_max=5)
    # 5 unauthenticated POSTs allowed (each returns 401), 6th comes back 429
    for _ in range(5):
        resp = client.post("/api/v1/measurements", json={})
        assert resp.status_code == 401
    blocked = client.post("/api/v1/measurements", json={})
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")


def test_api_ratelimit_runs_before_auth(app, client):
    """Even with a VALID api key + signature, rate limit kicks first if
    the bucket is exhausted. Otherwise an attacker can't get throttled
    before brute-forcing the auth check."""
    _enable_ratelimit(app, api_max=2)
    app.config["API_KEYS"] = {"k1": "secret"}

    body = b'{"metric":"temperature","temperature":1}'
    sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    headers = {"X-API-Key": "k1", "X-Signature": sig, "Content-Type": "application/json"}

    # 2 valid POSTs allowed
    r1 = client.post("/api/v1/measurements", data=body, headers=headers)
    r2 = client.post("/api/v1/measurements", data=body, headers=headers)
    assert r1.status_code in (200, 400)  # depends on payload validity
    assert r2.status_code in (200, 400)
    # 3rd, even with valid auth, should hit rate limit
    r3 = client.post("/api/v1/measurements", data=body, headers=headers)
    assert r3.status_code == 429


def test_x_forwarded_for_used_for_identity(app, client):
    """Behind a reverse proxy, the rate-limit ident comes from XFF.
    Two distinct XFF values get independent buckets."""
    _enable_ratelimit(app)
    headers_a = {"X-Forwarded-For": "10.0.0.1"}
    headers_b = {"X-Forwarded-For": "10.0.0.2"}
    # Exhaust IP A
    for _ in range(10):
        resp = client.get("/auth/login", headers=headers_a)
        assert resp.status_code == 200
    blocked_a = client.get("/auth/login", headers=headers_a)
    assert blocked_a.status_code == 429
    # IP B is untouched
    fresh_b = client.get("/auth/login", headers=headers_b)
    assert fresh_b.status_code == 200


def test_ratelimit_disabled_by_default_in_tests(app, client):
    """Sanity: conftest disables rate limit so existing tests don't
    accidentally trip it. 200 valid logins should all sail through."""
    # NOTE: NOT calling _enable_ratelimit
    for _ in range(50):
        resp = client.get("/auth/login")
        assert resp.status_code == 200


def test_ratelimit_fails_open_when_redis_unreachable(app, client, monkeypatch):
    """Redis going down must NOT 500 the login page. The limiter logs a
    warning and lets the request through; orchestration takes the
    instance out of rotation via /readyz."""
    _enable_ratelimit(app)

    from app.services import ratelimit

    def boom(*_a, **_k):
        raise ConnectionError("redis went away")

    monkeypatch.setattr(ratelimit, "get_redis", boom)

    # Hammer past the configured limit — every request should still
    # land 200 because the limiter fails open.
    for _ in range(20):
        resp = client.get("/auth/login")
        assert resp.status_code == 200, "limiter must fail open when Redis is down"
