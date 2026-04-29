"""Outbound webhook job: HTTP POST with optional HMAC-SHA256 signing.

Designed to run inside an RQ worker so retries + DLQ are managed by the
queue (see `app/services/queue.py`). The function deliberately raises on
non-2xx responses and on transport errors so RQ's `Retry` policy kicks
in. Final exhaustion drops the job into the failed-job registry, which
serves as the DLQ until a separate inspection UI is built.

Mirrors the inbound `/api/v1/measurements` HMAC scheme: the SHA-256 hex
signature of the canonicalised JSON body is sent as `X-QMS-Signature:
sha256=<hex>` alongside `X-QMS-Timestamp` (Unix seconds).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10


def post_webhook(
    url: str,
    payload: dict[str, Any],
    secret: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """POST `payload` as JSON to `url`. Sign with `secret` when provided.

    Returns a small dict on success (`{"status_code": ..., "body_size": ...}`).
    Raises `requests.HTTPError` on non-2xx and `requests.RequestException`
    on transport failure — both surface to RQ as job failures.
    """
    import requests

    body = json.dumps(payload, default=str, sort_keys=True).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "qms-webhook/1.0",
    }
    if secret:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-QMS-Signature"] = f"sha256={signature}"
        headers["X-QMS-Timestamp"] = str(int(time.time()))

    logger.info("Webhook POST → %s (%d bytes)", url, len(body))
    response = requests.post(url, data=body, headers=headers, timeout=timeout)
    response.raise_for_status()
    return {"status_code": response.status_code, "body_size": len(response.content)}
