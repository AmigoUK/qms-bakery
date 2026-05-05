"""Outbound SMS job: ClickSend REST v3 send.

Runs inside an RQ worker so retries + DLQ are managed by the queue (see
`app/services/queue.py`). Same retry envelope as the webhook + email
jobs: 3 / 9 / 27 minute backoff, then DLQ.

ClickSend API:
  POST https://rest.clicksend.com/v3/sms/send
  Auth: HTTP Basic (username + API key)
  Body: {"messages": [{"to": "+...", "source": "QMS", "body": "..."}, ...]}

Retry policy:
  - 5xx, 429, network failure → raise to retry
  - 4xx (auth, validation) → raise non-retryable so it lands in DLQ on first hit
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15
SMS_MAX_BODY_CHARS = 1530  # ClickSend caps at ~10 concatenated SMS segments


class PermanentSMSError(Exception):
    """Raised on 4xx — config/payload error, retrying won't help."""


def send_sms(
    to: list[str] | str,
    body: str,
    *,
    username: str,
    api_key: str,
    source: str = "QMS",
    base_url: str = "https://rest.clicksend.com/v3",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Send the same body to one or more E.164 numbers via ClickSend.

    Returns a small success dict; raises on transport failure or
    non-2xx so RQ can retry.
    """
    import requests

    if isinstance(to, str):
        to = [to]
    to = [n for n in (s.strip() for s in to) if n]
    if not to:
        raise ValueError("send_sms requires at least one recipient")
    if not username or not api_key:
        raise ValueError("send_sms requires ClickSend username + api_key")
    if not body:
        raise ValueError("send_sms requires a non-empty body")

    payload = {
        "messages": [
            {"to": number, "source": source, "body": body[:SMS_MAX_BODY_CHARS]}
            for number in to
        ]
    }
    url = f"{base_url.rstrip('/')}/sms/send"
    logger.info("ClickSend SMS → %s recipient(s) via %s", len(to), url)
    response = requests.post(
        url, json=payload, auth=(username, api_key), timeout=timeout
    )
    # 4xx (except 429) is permanent: bad creds / bad number / out of credit.
    # Raise a non-RequestException so RQ does not consume a retry budget.
    if 400 <= response.status_code < 500 and response.status_code != 429:
        # Drain remaining retries on the running job so RQ drops the job
        # straight to the failed-job registry rather than burning the
        # 3/9/27-minute backoff envelope on something that won't fix
        # itself.
        from app.jobs.webhook import _drain_retries

        _drain_retries()
        raise PermanentSMSError(
            f"ClickSend rejected SMS ({response.status_code}): {response.text[:200]}"
        )
    response.raise_for_status()
    return {"recipients": to, "status_code": response.status_code}
