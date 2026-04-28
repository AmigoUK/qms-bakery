"""External REST API for IoT/ERP integrations.

Authenticates via `X-API-Key` + HMAC-SHA256 signature in `X-Signature` over
the raw request body. Posts to `/api/v1/measurements` are the standard way
for non-MQTT devices and ERPs to feed metrics into the trigger engine.

Configuration: API keys + secrets are stored in the env / config dict
`API_KEYS` -> {key_id: secret}. In production this would be a proper
table; for now it lives in app.config so we don't depend on extra schema.
"""

from __future__ import annotations

import hashlib
import hmac

from flask import Blueprint, current_app, jsonify, request

from app.extensions import csrf, db
from app.services import triggers as trigger_service

bp = Blueprint("api", __name__)
csrf.exempt(bp)  # external clients can't carry CSRF tokens


def _verify_signature() -> bool:
    api_key = request.headers.get("X-API-Key", "")
    signature = request.headers.get("X-Signature", "")
    keys: dict[str, str] = current_app.config.get("API_KEYS", {})
    secret = keys.get(api_key)
    if not secret or not signature:
        return False
    body = request.get_data() or b""
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@bp.before_request
def _auth():
    if not _verify_signature():
        return jsonify({"error": "unauthorized"}), 401
    return None


@bp.route("/v1/measurements", methods=["POST"])
def post_measurement():
    """Ingest one metric reading. Triggers are evaluated synchronously.

    Body shape:
        {"metric": "temperature", "temperature": 232.5,
         "scope": "line:LINE_A", "line_id": "<uuid>",
         "device_id": "oven-1", "source": "iot"}
    """
    payload = request.get_json(silent=True) or {}
    if "metric" not in payload:
        return jsonify({"error": "metric required"}), 400

    fired = trigger_service.evaluate(payload)
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "triggers_fired": [
                {
                    "trigger_code": ex.trigger.code,
                    "ticket_id": ex.linked_ticket_id,
                    "responder_results": ex.responder_results,
                }
                for ex in fired
            ],
        }
    )


@bp.route("/v1/health", methods=["GET"])
def health():
    return jsonify({"ok": True})
