"""Outbound webhook job: HTTP POST with optional HMAC-SHA256 signing.

Designed to run inside an RQ worker so retries + DLQ are managed by the
queue (see `app/services/queue.py`). The function deliberately raises on
non-2xx responses and on transport errors so RQ's `Retry` policy kicks
in. Final exhaustion drops the job into the failed-job registry, which
serves as the DLQ until a separate inspection UI is built.

Mirrors the inbound `/api/v1/measurements` HMAC scheme: the SHA-256 hex
signature of the canonicalised JSON body is sent as `X-QMS-Signature:
sha256=<hex>` alongside `X-QMS-Timestamp` (Unix seconds).

SSRF guard
----------
Before any HTTP call we resolve the target host and reject loopback,
link-local, RFC1918 / unique-local, and unspecified addresses. This stops
a misconfigured (or malicious) admin-defined responder from probing
cloud-metadata endpoints (`169.254.169.254`) or internal services
(`localhost:6379`, `10.0.0.0/8`). The check raises
`UnsafeWebhookURL` — a permanent error that RQ shouldn't retry.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10


class UnsafeWebhookURL(ValueError):
    """Raised when a webhook target resolves to a non-routable address.

    Permanent — retrying with the same URL won't help, so RQ should drop
    the job to the DLQ rather than burning the retry budget.
    """


def _is_safe_outbound_host(hostname: str) -> bool:
    """Return True if `hostname` resolves only to public unicast addresses.

    Conservative: any single resolution to a private/loopback/link-local
    address fails the check, even if other answers are public. DNS
    rebinding is the reason — we don't want a hostname that *currently*
    answers public to be trusted, then re-resolve to private at request
    time. (`requests` re-resolves on its own connect call too, so a
    pure pre-flight check is best-effort, but it catches the vast
    majority of misconfigurations.)
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _validate_outbound_url(url: str, *, allow_private: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeWebhookURL(f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeWebhookURL("missing hostname")
    if allow_private:
        return
    if not _is_safe_outbound_host(parsed.hostname):
        raise UnsafeWebhookURL(
            f"host {parsed.hostname!r} resolves to a non-routable address"
        )


def post_webhook(
    url: str,
    payload: dict[str, Any],
    secret: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    allow_private: bool = False,
) -> dict[str, Any]:
    """POST `payload` as JSON to `url`. Sign with `secret` when provided.

    Returns a small dict on success (`{"status_code": ..., "body_size": ...}`).
    Raises `requests.HTTPError` on non-2xx, `requests.RequestException`
    on transport failure (both surface to RQ as transient job failures),
    and `UnsafeWebhookURL` when SSRF guard rejects the URL (permanent —
    job goes straight to the DLQ).

    `allow_private` is for tests / internal diagnostics; production code
    should never set it. The default-deny stance is what makes the SSRF
    guard worth having.
    """
    import requests

    _validate_outbound_url(url, allow_private=allow_private)

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
