"""Liveness + readiness probes."""

from __future__ import annotations


def test_healthz_returns_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_healthz_does_not_require_auth(client):
    """Healthcheck must work without a session — orchestrators don't log in."""
    # Ensure no session cookie present
    client.cookie_jar.clear() if hasattr(client, "cookie_jar") else None
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_readyz_returns_ok_when_dependencies_reachable(client):
    """Both fakeredis (injected) + sqlite (in-memory) are alive in tests."""
    resp = client.get("/readyz")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "ok"
    assert payload["checks"]["db"] == "ok"
    assert payload["checks"]["redis"] == "ok"


def test_readyz_fails_when_redis_unreachable(app, client):
    """When Redis ping raises, /readyz reports degraded with 503 so the
    orchestrator stops routing traffic instead of queuing it."""

    class _BadRedis:
        def ping(self):
            raise RuntimeError("redis down")

    app.config["REDIS_CLIENT"] = _BadRedis()
    resp = client.get("/readyz")
    assert resp.status_code == 503
    payload = resp.get_json()
    assert payload["status"] == "degraded"
    assert payload["checks"]["db"] == "ok"
    assert payload["checks"]["redis"].startswith("error:")
