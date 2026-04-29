"""ClickSend SMS job: payload shape + auth + retry classification.

`requests.post` is monkeypatched so the test never opens a socket.
"""

from __future__ import annotations

import pytest
import requests

from app.jobs import sms as sms_job
from app.services import queue as queue_service


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "{}"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return {}


class _RecordingPost:
    """Captures the last call to requests.post."""

    def __init__(self, status_code: int = 200, text: str = '{"http_code":200}'):
        self.status_code = status_code
        self.text = text
        self.calls: list[dict] = []

    def __call__(self, url, json=None, auth=None, timeout=None, **kw):
        self.calls.append(
            {"url": url, "json": json, "auth": auth, "timeout": timeout, **kw}
        )
        return _FakeResponse(self.status_code, self.text)


def test_send_sms_posts_to_clicksend_endpoint_with_basic_auth(monkeypatch):
    rec = _RecordingPost(200)
    monkeypatch.setattr(requests, "post", rec)

    result = sms_job.send_sms(
        to=["+447700900000", "+447700900001"],
        body="Trigger fired",
        username="api-user",
        api_key="api-key",
        source="QMS",
    )
    assert result == {
        "recipients": ["+447700900000", "+447700900001"],
        "status_code": 200,
    }
    assert len(rec.calls) == 1
    call = rec.calls[0]
    # Endpoint per ClickSend REST v3: /sms/send under the configured base URL
    assert call["url"] == "https://rest.clicksend.com/v3/sms/send"
    # HTTP Basic auth via requests' tuple form
    assert call["auth"] == ("api-user", "api-key")
    # ClickSend "messages" array shape
    body = call["json"]
    assert "messages" in body
    msgs = body["messages"]
    assert len(msgs) == 2
    assert {m["to"] for m in msgs} == {"+447700900000", "+447700900001"}
    assert all(m["source"] == "QMS" and m["body"] == "Trigger fired" for m in msgs)


def test_send_sms_normalises_string_recipient(monkeypatch):
    rec = _RecordingPost(200)
    monkeypatch.setattr(requests, "post", rec)
    sms_job.send_sms(
        to="+447700900000",
        body="hi",
        username="u",
        api_key="k",
    )
    body = rec.calls[0]["json"]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["to"] == "+447700900000"


def test_send_sms_truncates_oversize_body(monkeypatch):
    rec = _RecordingPost(200)
    monkeypatch.setattr(requests, "post", rec)
    huge = "x" * 5000
    sms_job.send_sms(
        to="+447700900000", body=huge, username="u", api_key="k"
    )
    sent = rec.calls[0]["json"]["messages"][0]["body"]
    assert len(sent) == sms_job.SMS_MAX_BODY_CHARS


def test_send_sms_rejects_empty_recipients(monkeypatch):
    monkeypatch.setattr(requests, "post", _RecordingPost(200))
    with pytest.raises(ValueError, match="recipient"):
        sms_job.send_sms(to=[], body="hi", username="u", api_key="k")


def test_send_sms_rejects_missing_credentials(monkeypatch):
    monkeypatch.setattr(requests, "post", _RecordingPost(200))
    with pytest.raises(ValueError, match="username"):
        sms_job.send_sms(to="+44...", body="hi", username="", api_key="k")


def test_send_sms_rejects_empty_body(monkeypatch):
    monkeypatch.setattr(requests, "post", _RecordingPost(200))
    with pytest.raises(ValueError, match="body"):
        sms_job.send_sms(to="+44...", body="", username="u", api_key="k")


def test_send_sms_4xx_raises_permanent(monkeypatch):
    """4xx (not 429) is a permanent rejection - bad creds, bad number, no
    credit. We raise a non-RequestException so RQ does not consume the
    retry budget on something a retry can't fix."""
    monkeypatch.setattr(requests, "post", _RecordingPost(401, '{"error":"unauth"}'))
    with pytest.raises(sms_job.PermanentSMSError):
        sms_job.send_sms(
            to="+447700900000", body="hi", username="u", api_key="k"
        )


def test_send_sms_429_raises_for_retry(monkeypatch):
    """429 is transient - rate limit. Should bubble HTTPError so RQ retries."""
    monkeypatch.setattr(requests, "post", _RecordingPost(429, '{"error":"rate"}'))
    with pytest.raises(requests.HTTPError):
        sms_job.send_sms(
            to="+447700900000", body="hi", username="u", api_key="k"
        )


def test_send_sms_5xx_raises_for_retry(monkeypatch):
    """5xx is transient - upstream issue. RQ retries via raise_for_status."""
    monkeypatch.setattr(requests, "post", _RecordingPost(503, '{"error":"down"}'))
    with pytest.raises(requests.HTTPError):
        sms_job.send_sms(
            to="+447700900000", body="hi", username="u", api_key="k"
        )


def test_enqueue_sms_carries_retry_policy(app):
    """The enqueued SMS job must have the 3/9/27 minute retry intervals."""
    with app.app_context():
        job = queue_service.enqueue_sms(to=["+447700900000"], body="hi")
        assert job.retries_left == 3
        assert job.retry_intervals == [180, 540, 1620]


def test_enqueue_sms_freezes_clicksend_config_into_kwargs(app):
    """A later credential rotation must not silently retarget queued jobs."""
    with app.app_context():
        app.config["CLICKSEND_USERNAME"] = "user"
        app.config["CLICKSEND_API_KEY"] = "secret"
        app.config["CLICKSEND_SOURCE"] = "BAKERY"
        app.config["CLICKSEND_BASE_URL"] = "https://rest.clicksend.com/v3"
        job = queue_service.enqueue_sms(to=["+447700900000"], body="hi")
        assert job.kwargs["username"] == "user"
        assert job.kwargs["api_key"] == "secret"
        assert job.kwargs["source"] == "BAKERY"
        assert job.kwargs["base_url"] == "https://rest.clicksend.com/v3"
