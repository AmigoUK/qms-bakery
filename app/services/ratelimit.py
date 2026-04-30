"""Per-IP rate limiting with Redis fixed-window counters.

A fixed-window counter is dumber than a sliding window but considerably
simpler and correct enough for our threat model: "stop a single IP from
flooding /auth/login or /api/v1/measurements". The price is allowing
2× the intended rate at window boundaries — acceptable for a 60s
window protecting a login form.

Per-IP key shape: `ratelimit:<bucket>:<ident>:<window_index>` with a
TTL of `window_seconds` so keys auto-clean.

Use as:

    from app.services.ratelimit import rate_limit

    @rate_limit(bucket="login", max_requests=10, window_seconds=60)
    def login():
        ...

The decorator returns a 429 with `Retry-After` when the bucket is full;
otherwise the wrapped view runs normally. Tests bypass via the
`RATELIMIT_ENABLED=False` config.
"""

from __future__ import annotations

import functools
import logging
import os
import time
from typing import Callable

from flask import current_app, jsonify, make_response, request

from app.services.stream import get_redis

_REDIS_KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "qms")

logger = logging.getLogger(__name__)


def _ident_for_request() -> str:
    """Extract the client identity for rate-limit bucketing.

    Honours `X-Forwarded-For` (left-most entry) when behind a reverse
    proxy. The request must come through a configured proxy chain in
    production for this to be trustworthy.
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def check(
    bucket: str,
    ident: str,
    *,
    max_requests: int,
    window_seconds: int,
) -> tuple[bool, int, int]:
    """Increment the counter for `(bucket, ident)` in the current window.

    Returns `(allowed, remaining, retry_after_seconds)`.

    Fails open when Redis is unreachable. Rate limiting is defence-in-
    depth — preferable to log-and-allow than to 500 the login page when
    Redis goes down. The `/readyz` probe still returns degraded so the
    orchestrator can take the instance out of rotation.
    """
    try:
        r = get_redis()
        now = int(time.time())
        window_index = now // window_seconds
        key = f"{_REDIS_KEY_PREFIX}:ratelimit:{bucket}:{ident}:{window_index}"
        count = r.incr(key)
        if count == 1:
            # First request in this window — set the TTL so the key
            # cleans itself up. Refreshing on every hit would let an
            # attacker keep a key alive forever.
            r.expire(key, window_seconds)
        allowed = count <= max_requests
        remaining = max(0, max_requests - count)
        retry_after = (window_index + 1) * window_seconds - now
        return allowed, remaining, retry_after
    except Exception as exc:
        logger.warning(
            "rate-limit backend unavailable (bucket=%s): %s — failing open",
            bucket,
            exc,
        )
        return True, max_requests, window_seconds


def rate_limit(
    *, bucket: str, max_requests: int, window_seconds: int = 60
) -> Callable:
    """View decorator. No-op when `RATELIMIT_ENABLED` is False (default
    True in production, override in tests)."""

    def _decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def _wrapped(*args, **kwargs):
            if not current_app.config.get("RATELIMIT_ENABLED", True):
                return view(*args, **kwargs)
            allowed, remaining, retry_after = check(
                bucket,
                _ident_for_request(),
                max_requests=max_requests,
                window_seconds=window_seconds,
            )
            if not allowed:
                resp = jsonify(
                    {
                        "error": "rate_limited",
                        "retry_after": retry_after,
                    }
                )
                resp.status_code = 429
                resp.headers["Retry-After"] = str(retry_after)
                resp.headers["X-RateLimit-Remaining"] = "0"
                return resp
            response = make_response(view(*args, **kwargs))
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            return response

        return _wrapped

    return _decorator
