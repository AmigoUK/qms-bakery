"""Dead-letter-queue inspection + replay over RQ's failed-job registries.

Both async responder queues (`qms:webhooks`, `qms:emails`) drop their
exhausted jobs into RQ's per-queue `FailedJobRegistry`. This module is
the read/manage surface for those:

  * `list_failed()`     — flatten both registries into a single ordered list
  * `get_failed(job_id)` — fetch a single job + its traceback
  * `requeue(job_id)`    — push a failed job back onto its origin queue
  * `discard(job_id)`    — drop a failed job from the registry permanently

We deliberately do not re-run jobs in-process; `requeue` puts them back
on the queue so the regular RQ worker handles execution + retry policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from flask import Flask
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.registry import FailedJobRegistry

from app.services import queue as queue_service


@dataclass
class FailedJobView:
    """Light, template-friendly snapshot of an RQ job in failed state."""

    id: str
    queue: str
    func_name: str
    enqueued_at: str | None
    failed_at: str | None
    exc_summary: str
    kwargs: dict

    @classmethod
    def from_job(cls, job: Job, queue_name: str) -> "FailedJobView":
        exc = (job.exc_info or "").strip().splitlines()
        summary = exc[-1] if exc else ""
        return cls(
            id=job.id,
            queue=queue_name,
            func_name=job.func_name or "",
            enqueued_at=_isoformat(job.enqueued_at),
            failed_at=_isoformat(job.ended_at),
            exc_summary=summary[:500],
            kwargs={k: _safe(v) for k, v in (job.kwargs or {}).items()},
        )


def _isoformat(dt) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _safe(v):
    """Redact obvious credential-like values when listing kwargs."""
    if isinstance(v, str) and len(v) > 200:
        return v[:200] + "…"
    return v


def _queues(app: Flask | None = None) -> list[Queue]:
    return [queue_service.get_queue(app), queue_service.get_email_queue(app)]


def list_failed(app: Flask | None = None) -> list[FailedJobView]:
    """All failed jobs across webhook + email queues, newest-first."""
    out: list[FailedJobView] = []
    for q in _queues(app):
        registry = FailedJobRegistry(queue=q)
        for jid in registry.get_job_ids():
            try:
                job = Job.fetch(jid, connection=q.connection)
            except NoSuchJobError:
                continue
            out.append(FailedJobView.from_job(job, q.name))
    out.sort(key=lambda v: v.failed_at or "", reverse=True)
    return out


def _resolve(app: Flask | None, job_id: str) -> tuple[Job, Queue, FailedJobRegistry]:
    """Locate `job_id` in either failed registry. Raises NoSuchJobError."""
    for q in _queues(app):
        registry = FailedJobRegistry(queue=q)
        if job_id in registry.get_job_ids():
            return Job.fetch(job_id, connection=q.connection), q, registry
    raise NoSuchJobError(f"Failed job {job_id} not found")


def get_failed(job_id: str, app: Flask | None = None) -> tuple[Job, str]:
    """Fetch full Job + originating queue name. Raises NoSuchJobError."""
    job, q, _ = _resolve(app, job_id)
    return job, q.name


def requeue(job_id: str, app: Flask | None = None) -> Job:
    """Move a failed job back onto its origin queue. Returns the job."""
    job, q, registry = _resolve(app, job_id)
    registry.requeue(job_id)
    return Job.fetch(job_id, connection=q.connection)


def discard(job_id: str, app: Flask | None = None) -> str:
    """Permanently drop a failed job from the registry. Returns queue name."""
    job, q, registry = _resolve(app, job_id)
    registry.remove(job, delete_job=True)
    return q.name


def counts(app: Flask | None = None) -> dict[str, int]:
    """Per-queue failed-job count, suitable for the admin overview."""
    return {q.name: FailedJobRegistry(queue=q).count for q in _queues(app)}
