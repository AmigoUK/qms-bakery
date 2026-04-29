"""Liveness + readiness probes for orchestration platforms.

  * `/healthz` — always 200 once the process can respond. Used by load
    balancers / Docker healthcheck to decide if the container is alive.
  * `/readyz`  — 200 only when DB + Redis are reachable. Used by
    orchestrators to decide whether to route traffic. Fails-closed (503)
    on any dependency error so traffic stops rather than queues up.

Both endpoints are CSRF-exempt + login-exempt so probes work without
auth state. Responses are tiny JSON so log lines stay readable.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from app.extensions import db

bp = Blueprint("health", __name__)


@bp.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


@bp.route("/readyz")
def readyz():
    checks: dict[str, str] = {}
    overall_ok = True

    try:
        db.session.execute(db.text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:  # pragma: no cover - defensive
        checks["db"] = f"error: {type(exc).__name__}"
        overall_ok = False

    try:
        client = current_app.config.get("REDIS_CLIENT")
        if client is None:
            from app.services.stream import get_redis

            client = get_redis()
        client.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # pragma: no cover - defensive
        checks["redis"] = f"error: {type(exc).__name__}"
        overall_ok = False

    status = 200 if overall_ok else 503
    return jsonify({"status": "ok" if overall_ok else "degraded", "checks": checks}), status
