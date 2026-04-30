"""Routine training-recert scheduler.

Long-running CLI loop. On each tick:
  1. For every active TrainingCourse with at least one Assignment,
  2. find every active Trainee matching any assignment's role/line filter,
  3. for whom there is no live cert AND no open enrolment for that course,
  4. enrol them — which signs a magic-link token and enqueues an SMS via
     the existing ClickSend queue.

Restart-safe: the `has_open_enrolment` guard makes a re-issue a no-op if
an SMS is already in flight, so a process restart can't double-text a
worker.

Entry-point: `flask training-scheduler` (registered in app/__init__.py).
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

from flask import Flask

from app.extensions import db
from app.models import TrainingCourse
from app.services import training as training_service

logger = logging.getLogger(__name__)


def _poll_interval(app: Flask) -> int:
    return int(
        os.environ.get(
            "TRAINING_SCHEDULER_POLL_SECONDS",
            str(app.config.get("TRAINING_SCHEDULER_POLL_SECONDS", 60)),
        )
    )


def run_once(app: Flask) -> dict[str, Any]:
    """Single tick. Returns a small summary dict (handy for testing)."""
    issued: list[dict] = []
    with app.app_context():
        courses = TrainingCourse.query.filter_by(is_active=True).all()
        for course in courses:
            try:
                trainees = list(training_service.trainees_due_for_course(course))
            except Exception:
                logger.exception(
                    "training-scheduler: failed to compute due trainees for %s",
                    course.code,
                )
                continue
            for trainee in trainees:
                try:
                    training_service.enrol(
                        trainee=trainee,
                        course=course,
                        source="schedule",
                    )
                    issued.append(
                        {"trainee_id": trainee.id, "course_code": course.code}
                    )
                except training_service.TrainingError as exc:
                    logger.warning(
                        "training-scheduler: skipped %s/%s: %s",
                        trainee.phone,
                        course.code,
                        exc,
                    )
                    db.session.rollback()
                    continue
            db.session.commit()
    return {"issued": len(issued), "items": issued}


_stop_requested = False


def _handle_signal(_sig, _frame):
    global _stop_requested
    _stop_requested = True


def run(app: Flask) -> None:
    interval = _poll_interval(app)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    logger.info(
        "Training scheduler starting: poll_interval=%ss", interval
    )
    while not _stop_requested:
        try:
            summary = run_once(app)
            if summary["issued"]:
                logger.info(
                    "training-scheduler tick: %d enrolment(s) issued",
                    summary["issued"],
                )
        except Exception:
            logger.exception("training-scheduler: tick failed")
        for _ in range(interval):
            if _stop_requested:
                break
            time.sleep(1)
    logger.info("Training scheduler stopped")
