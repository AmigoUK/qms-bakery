"""Permanent errors short-circuit the RQ retry budget.

UnsafeWebhookURL and PermanentSMSError must drop the job to the
failed-job registry on the first failure, not consume the 3/9/27-min
backoff envelope.
"""

from __future__ import annotations

from unittest import mock

from app.jobs import sms as sms_job
from app.jobs import webhook as webhook_job


class _FakeJob:
    def __init__(self):
        self.retries_left = 3


def test_unsafe_webhook_url_drains_retries():
    fake = _FakeJob()
    with mock.patch("rq.get_current_job", return_value=fake):
        try:
            webhook_job.post_webhook("http://127.0.0.1:9999/x", payload={})
        except webhook_job.UnsafeWebhookURL:
            pass
        else:
            raise AssertionError("expected UnsafeWebhookURL")
    assert fake.retries_left == 0


def test_permanent_sms_error_drains_retries():
    fake = _FakeJob()
    response = mock.Mock()
    response.status_code = 400
    response.text = "bad credentials"
    fake_requests = mock.Mock()
    fake_requests.post.return_value = response
    with (
        mock.patch("rq.get_current_job", return_value=fake),
        mock.patch.dict("sys.modules", {"requests": fake_requests}),
    ):
        try:
            sms_job.send_sms(
                to="+447700000001",
                body="hi",
                username="u",
                api_key="k",
            )
        except sms_job.PermanentSMSError:
            pass
        else:
            raise AssertionError("expected PermanentSMSError")
    assert fake.retries_left == 0


def test_drain_retries_no_op_outside_worker():
    """When called outside an RQ worker, _drain_retries silently no-ops
    rather than crashing."""
    # Simulate "no current job" — RQ's get_current_job returns None.
    with mock.patch("rq.get_current_job", return_value=None):
        webhook_job._drain_retries()  # must not raise
