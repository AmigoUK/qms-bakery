"""DLQ inspection + replay over RQ's FailedJobRegistry.

To produce a real failed job we enqueue with `Retry(max=0)` so the very
first failure parks the job straight into the failed registry, then
drive a `SimpleWorker` over it once. No real network or SMTP touched —
the job target is a module-level helper that just raises.
"""

from __future__ import annotations

import pytest
from rq import SimpleWorker
from rq.registry import FailedJobRegistry

from app.services import dlq as dlq_service
from app.services import queue as queue_service


def _always_fails(reason: str = "boom") -> None:
    raise RuntimeError(reason)


def _enqueue_failing(app, *, queue_name: str = "qms:webhooks") -> str:
    """Enqueue a job that fails, then run SimpleWorker once. Returns the
    failed job id."""
    if queue_name == queue_service.QUEUE_NAME:
        q = queue_service.get_queue(app)
    else:
        q = queue_service.get_email_queue(app)
    # No Retry → first failure goes straight into FailedJobRegistry
    job = q.enqueue(
        "tests.test_dlq._always_fails",
        kwargs={"reason": "boom"},
    )
    worker = SimpleWorker([q], connection=q.connection)
    worker.work(burst=True, with_scheduler=False)
    assert job.id in FailedJobRegistry(queue=q).get_job_ids(), (
        "expected job to land in failed registry"
    )
    return job.id


def test_list_failed_includes_jobs_from_both_queues(app):
    with app.app_context():
        wh_id = _enqueue_failing(app, queue_name="qms:webhooks")
        em_id = _enqueue_failing(app, queue_name="qms:emails")
        rows = dlq_service.list_failed()
        ids = {r.id for r in rows}
        assert wh_id in ids
        assert em_id in ids
        # Counts split per queue
        c = dlq_service.counts()
        assert c["qms:webhooks"] >= 1
        assert c["qms:emails"] >= 1


def test_get_failed_returns_job_with_traceback(app):
    with app.app_context():
        jid = _enqueue_failing(app)
        job, queue_name = dlq_service.get_failed(jid)
        assert queue_name == "qms:webhooks"
        assert job.id == jid
        assert "RuntimeError" in (job.exc_info or "")
        assert "boom" in (job.exc_info or "")


def test_requeue_moves_job_back_onto_origin_queue(app):
    with app.app_context():
        jid = _enqueue_failing(app, queue_name="qms:emails")
        em_q = queue_service.get_email_queue(app)
        assert jid not in em_q.get_job_ids()  # in failed registry, not the live queue

        dlq_service.requeue(jid)

        # Now back on the live queue
        assert jid in em_q.get_job_ids()
        # And gone from the failed registry
        assert jid not in FailedJobRegistry(queue=em_q).get_job_ids()


def test_discard_removes_job_permanently(app):
    with app.app_context():
        jid = _enqueue_failing(app)
        wh_q = queue_service.get_queue(app)
        registry = FailedJobRegistry(queue=wh_q)
        assert jid in registry.get_job_ids()

        queue_name = dlq_service.discard(jid)
        assert queue_name == "qms:webhooks"
        assert jid not in registry.get_job_ids()


def test_get_failed_unknown_id_raises(app):
    from rq.exceptions import NoSuchJobError

    with app.app_context():
        with pytest.raises(NoSuchJobError):
            dlq_service.get_failed("does-not-exist")


def test_dlq_index_route_lists_failed_jobs(app, client, login_admin):
    with app.app_context():
        jid = _enqueue_failing(app)
    login_admin()
    resp = client.get("/admin/dlq")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert jid in body
    assert "qms:webhooks" in body


def test_dlq_requeue_route_requeues_and_audits(app, client, login_admin):
    from app.extensions import db
    from app.models.audit import AuditLog

    with app.app_context():
        jid = _enqueue_failing(app, queue_name="qms:emails")
    login_admin()
    resp = client.post(f"/admin/dlq/{jid}/requeue", follow_redirects=False)
    assert resp.status_code == 302
    with app.app_context():
        em_q = queue_service.get_email_queue(app)
        assert jid in em_q.get_job_ids()
        # audit row recorded
        row = (
            db.session.query(AuditLog)
            .filter_by(action="dlq_requeue", entity_id=jid)
            .first()
        )
        assert row is not None


def test_dlq_discard_route_removes_and_audits(app, client, login_admin):
    from app.extensions import db
    from app.models.audit import AuditLog

    with app.app_context():
        jid = _enqueue_failing(app)
    login_admin()
    resp = client.post(f"/admin/dlq/{jid}/discard", follow_redirects=False)
    assert resp.status_code == 302
    with app.app_context():
        wh_q = queue_service.get_queue(app)
        assert jid not in FailedJobRegistry(queue=wh_q).get_job_ids()
        row = (
            db.session.query(AuditLog)
            .filter_by(action="dlq_discard", entity_id=jid)
            .first()
        )
        assert row is not None


def test_dlq_routes_require_permission(app, client):
    """Anonymous → redirected to login. Logged in without dlq.manage → 403."""
    resp = client.get("/admin/dlq")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
