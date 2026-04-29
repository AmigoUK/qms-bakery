"""Locust load-test harness for the QMS API.

Validates the README's Phase-3 throughput target: 200 tickets/min
sustained on the synchronous `/api/v1/measurements` path (HMAC-signed,
trigger evaluation in-band, audit + commit per request).

Usage
-----

    # 1. seed an API key on the target instance:
    flask shell <<<'
    from flask import current_app
    current_app.config["API_KEYS"] = {"loadtest": "loadtest-secret"}
    '
    # (or set API_KEYS env / config before boot — see app/__init__.py)

    # 2. lift the rate limit OR raise it for the source IP, otherwise
    #    Locust just measures 429 throughput:
    export RATELIMIT_API_MAX=100000

    # 3. run smoke (single user, 30s, baseline latency):
    locust -f loadtest/locustfile.py --headless \\
        -u 1 -r 1 -t 30s \\
        --host https://qms.local \\
        LoadtestApi

    # 4. run soak at the throughput target (50 users ~= 250 req/s peak):
    LOCUST_API_KEY=loadtest LOCUST_API_SECRET=loadtest-secret \\
    locust -f loadtest/locustfile.py --headless \\
        -u 50 -r 5 -t 5m \\
        --host https://qms.local \\
        LoadtestApi DashboardBrowser

Pass/fail target: P95 latency < 250 ms on /api/v1/measurements at
200 tickets/min sustained, with no 5xx and no 429s when the bucket
is sized correctly. See `loadtest/README.md` for interpretation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import uuid

from locust import HttpUser, between, task


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _measurement_payload(*, deviating: bool) -> bytes:
    """Either an in-spec reading or one that deviates.

    The exact CCP limits depend on what was seeded. We pick numbers
    that *typically* match a "freezer must stay below 4 °C" trigger and
    its inverse — devs can tweak per-deployment.
    """
    if deviating:
        temperature = round(random.uniform(8.0, 15.0), 2)
    else:
        temperature = round(random.uniform(0.5, 3.5), 2)
    payload = {
        "metric": "temperature",
        "temperature": temperature,
        "scope": "line:LINE_A",
        "device_id": f"loadtest-{uuid.uuid4().hex[:8]}",
        "source": "loadtest",
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class LoadtestApi(HttpUser):
    """High-throughput user hammering /api/v1/measurements.

    Each task signs a fresh payload and POSTs it. ~30 % of requests
    carry a deviating reading, exercising the trigger-fires + ticket
    creation + responder-enqueue branch.
    """

    wait_time = between(0.05, 0.2)  # ~5–20 req/s per user

    def on_start(self) -> None:
        self.api_key = os.environ.get("LOCUST_API_KEY", "loadtest")
        self.api_secret = os.environ.get("LOCUST_API_SECRET", "loadtest-secret")

    @task(7)
    def post_in_spec(self) -> None:
        body = _measurement_payload(deviating=False)
        self._post(body)

    @task(3)
    def post_deviation(self) -> None:
        body = _measurement_payload(deviating=True)
        self._post(body)

    def _post(self, body: bytes) -> None:
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "X-Signature": _sign(self.api_secret, body),
        }
        with self.client.post(
            "/api/v1/measurements",
            data=body,
            headers=headers,
            name="POST /api/v1/measurements",
            catch_response=True,
        ) as resp:
            if resp.status_code == 429:
                resp.failure("rate-limited — raise RATELIMIT_API_MAX or scale users down")
            elif resp.status_code >= 500:
                resp.failure(f"server error {resp.status_code}: {resp.text[:200]}")
            elif resp.status_code != 200:
                resp.failure(f"unexpected {resp.status_code}: {resp.text[:200]}")


class DashboardBrowser(HttpUser):
    """Realistic background read traffic — a logged-in operator looking
    at the tickets list while sensors are pushing data."""

    wait_time = between(2.0, 5.0)

    def on_start(self) -> None:
        email = os.environ.get("LOCUST_USER_EMAIL", "admin@local")
        password = os.environ.get("LOCUST_USER_PASSWORD", "ChangeMe123!")
        # GET first to grab a CSRF token from the login form.
        login_page = self.client.get("/auth/login", name="GET /auth/login")
        token = _extract_csrf(login_page.text)
        self.client.post(
            "/auth/login",
            data={"email": email, "password": password, "csrf_token": token or ""},
            name="POST /auth/login",
            allow_redirects=False,
        )

    @task(5)
    def list_tickets(self) -> None:
        self.client.get("/tickets/", name="GET /tickets")

    @task(2)
    def dashboard(self) -> None:
        self.client.get("/", name="GET /")

    @task(1)
    def health(self) -> None:
        self.client.get("/healthz", name="GET /healthz")


def _extract_csrf(html: str) -> str | None:
    marker = 'name="csrf_token"'
    idx = html.find(marker)
    if idx == -1:
        return None
    value_idx = html.find('value="', idx)
    if value_idx == -1:
        return None
    start = value_idx + len('value="')
    end = html.find('"', start)
    return html[start:end] if end > start else None
