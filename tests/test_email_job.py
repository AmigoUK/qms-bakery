"""Email job: SMTP build + send + retry policy.

`smtplib.SMTP` is monkeypatched so the test never opens a socket. We
verify the EmailMessage is built correctly (headers, plain + html
parts) and that `send_email` raises on empty recipients.
"""

from __future__ import annotations

import smtplib

import pytest

from app.jobs.email import _redact_email, send_email
from app.services import queue as queue_service


class _FakeSMTP:
    """Records every call so the test can assert on it."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_args: tuple | None = None
        self.sent_message = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, msg):
        self.sent_message = msg


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeSMTP.instances.clear()
    yield
    _FakeSMTP.instances.clear()


def test_send_email_builds_message_and_calls_smtp(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    result = send_email(
        to=["ops@example.com"],
        subject="Trigger fired",
        body_text="Plain text body",
        body_html="<p>HTML body</p>",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="user",
        smtp_password="pw",
        smtp_use_tls=True,
        sender="qms@example.com",
    )
    assert result == {"recipients": ["ops@example.com"], "subject": "Trigger fired"}
    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.com"
    assert smtp.port == 587
    assert smtp.starttls_called is True
    assert smtp.login_args == ("user", "pw")
    msg = smtp.sent_message
    assert msg is not None
    assert msg["From"] == "qms@example.com"
    assert msg["To"] == "ops@example.com"
    assert msg["Subject"] == "Trigger fired"
    # multipart/alternative when html present
    assert msg.is_multipart()
    parts = list(msg.iter_parts())
    subtypes = sorted(p.get_content_subtype() for p in parts)
    assert subtypes == ["html", "plain"]


def test_send_email_skips_login_without_credentials(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    send_email(
        to="single@example.com",
        subject="Hi",
        body_text="x",
        smtp_host="localhost",
        smtp_port=25,
        smtp_username=None,
        smtp_password=None,
        smtp_use_tls=False,
        sender="qms@local",
    )
    smtp = _FakeSMTP.instances[0]
    assert smtp.starttls_called is False
    assert smtp.login_args is None
    # Single string recipient is normalised to one-element list
    assert smtp.sent_message["To"] == "single@example.com"


def test_send_email_rejects_empty_recipients(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    with pytest.raises(ValueError, match="recipient"):
        send_email(
            to=[],
            subject="x",
            body_text="x",
            smtp_host="localhost",
            smtp_port=25,
            smtp_username=None,
            smtp_password=None,
            smtp_use_tls=False,
            sender="qms@local",
        )


def test_enqueue_email_carries_retry_policy(app):
    """The enqueued email job must have the 3/9/27 minute retry intervals
    declared so RQ schedules the right backoff."""
    with app.app_context():
        job = queue_service.enqueue_email(
            to=["ops@example.com"],
            subject="x",
            body_text="x",
        )
        assert job.retries_left == 3
        assert job.retry_intervals == [180, 540, 1620]


def test_redact_email_masks_local_part_keeps_domain():
    assert _redact_email("john.doe@company.com") == "j***@company.com"
    assert _redact_email("a@b.c") == "a***@b.c"
    # Malformed addresses get fully masked rather than half-leaked.
    assert _redact_email("nope") == "***"
    assert _redact_email("") == "***"


def test_send_email_logs_redact_recipients(monkeypatch, caplog):
    """The INFO log line must not contain raw recipient addresses."""
    import logging

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    with caplog.at_level(logging.INFO, logger="app.jobs.email"):
        send_email(
            to=["alice@example.com", "bob@example.com"],
            subject="hi",
            body_text="hello",
            smtp_host="smtp.test.local",
            smtp_port=25,
            smtp_username=None,
            smtp_password=None,
            smtp_use_tls=False,
            sender="qms@test.local",
        )
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "alice@example.com" not in log_text
    assert "bob@example.com" not in log_text
    # Domain still visible for delivery debugging.
    assert "example.com" in log_text


def test_enqueue_email_freezes_smtp_config_into_kwargs(app):
    """A later config change must not silently retarget queued jobs."""
    with app.app_context():
        app.config["SMTP_HOST"] = "smtp.test.local"
        app.config["SMTP_PORT"] = 2525
        app.config["SMTP_USERNAME"] = "u"
        app.config["SMTP_PASSWORD"] = "p"
        app.config["SMTP_USE_TLS"] = True
        app.config["SMTP_FROM"] = "qms@test.local"
        job = queue_service.enqueue_email(
            to=["a@b.c"], subject="s", body_text="b"
        )
        assert job.kwargs["smtp_host"] == "smtp.test.local"
        assert job.kwargs["smtp_port"] == 2525
        assert job.kwargs["smtp_username"] == "u"
        assert job.kwargs["smtp_password"] == "p"
        assert job.kwargs["smtp_use_tls"] is True
        assert job.kwargs["sender"] == "qms@test.local"
