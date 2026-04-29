"""`app.jobs.webhook.post_webhook`: HMAC signing + raise-on-non-2xx."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.jobs import webhook


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"ok"):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_post_webhook_sends_json_body(monkeypatch):
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["data"] = kwargs.get("data")
        captured["headers"] = kwargs.get("headers")
        captured["timeout"] = kwargs.get("timeout")
        return _FakeResponse(200)

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    result = webhook.post_webhook(
        "https://example.test/hook", {"alert": "overheat", "value": 232.5}
    )
    assert result == {"status_code": 200, "body_size": 2}
    assert captured["url"] == "https://example.test/hook"
    assert captured["headers"]["Content-Type"] == "application/json"
    body = json.loads(captured["data"])
    assert body == {"alert": "overheat", "value": 232.5}
    # No HMAC headers when secret is omitted
    assert "X-QMS-Signature" not in captured["headers"]


def test_post_webhook_signs_when_secret_provided(monkeypatch):
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return _FakeResponse(200)

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    webhook.post_webhook(
        "https://example.test/hook",
        {"x": 1},
        secret="super-secret-key",
    )
    body = captured["data"]
    expected_sig = hmac.new(
        b"super-secret-key", body, hashlib.sha256
    ).hexdigest()
    assert captured["headers"]["X-QMS-Signature"] == f"sha256={expected_sig}"
    assert captured["headers"]["X-QMS-Timestamp"].isdigit()


def test_post_webhook_raises_on_non_2xx(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "post", lambda *_a, **_k: _FakeResponse(503))
    with pytest.raises(requests.HTTPError):
        webhook.post_webhook("https://example.test/hook", {})


def test_post_webhook_propagates_transport_error(monkeypatch):
    import requests

    def boom(*_a, **_k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(requests.ConnectionError):
        webhook.post_webhook("https://example.test/hook", {})


def test_signature_is_deterministic_for_same_body(monkeypatch):
    """Signing canonicalises the JSON (sorted keys), so a webhook receiver
    can recompute the signature reliably without re-serialising us."""
    captured_bodies: list[bytes] = []

    def fake_post(url, **kwargs):
        captured_bodies.append(kwargs["data"])
        return _FakeResponse(200)

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    webhook.post_webhook("https://x", {"a": 1, "b": 2}, secret="k")
    webhook.post_webhook("https://x", {"b": 2, "a": 1}, secret="k")
    assert captured_bodies[0] == captured_bodies[1]
