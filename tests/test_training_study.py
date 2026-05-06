"""Optional study/practice step between modules and the exam.

Verifies:
- start POST + next_module POST now redirect into /study (not /exam)
- /study GET renders, lists every question and every option, and
  exposes the is_correct flag so the front-end JS can render
  ✓ / ✗ marks
- a SUBMITTED enrolment hitting /study is bounced to /result
"""

from __future__ import annotations

from app.extensions import db
from app.models import (
    EnrolmentStatus,
    TrainingAnswerOption,
    TrainingModule,
    TrainingQuestion,
)
from app.services import training as t


def _seed_course():
    course = t.create_course(code="HACCP-STUDY")
    version = t.add_course_version(
        course=course,
        title={"pl": "HACCP", "en": "HACCP"},
        summary={"pl": "", "en": ""},
        pass_threshold=0.5,
        link_ttl_days=7,
    )
    db.session.add(
        TrainingModule(
            course_version_id=version.id,
            order_index=0,
            title={"pl": "M", "en": "M"},
            body_md={"pl": "<p>Moduł</p>", "en": "<p>Module</p>"},
        )
    )
    # Two questions so we can prove the rendered page lists every one,
    # and the second has a distinct correct option so the assertion
    # isn't accidentally satisfied by a single duplicate.
    q1 = TrainingQuestion(
        course_version_id=version.id,
        order_index=0,
        prompt={"pl": "Pierwsze pytanie?", "en": "First question?"},
    )
    q2 = TrainingQuestion(
        course_version_id=version.id,
        order_index=1,
        prompt={"pl": "Drugie pytanie?", "en": "Second question?"},
    )
    db.session.add_all([q1, q2])
    db.session.flush()
    db.session.add_all(
        [
            TrainingAnswerOption(
                question_id=q1.id, order_index=0,
                label={"pl": "Tak-PL", "en": "Yes-EN"}, is_correct=True,
            ),
            TrainingAnswerOption(
                question_id=q1.id, order_index=1,
                label={"pl": "Nie-PL", "en": "No-EN"}, is_correct=False,
            ),
            TrainingAnswerOption(
                question_id=q2.id, order_index=0,
                label={"pl": "Alfa", "en": "Alpha"}, is_correct=False,
            ),
            TrainingAnswerOption(
                question_id=q2.id, order_index=1,
                label={"pl": "Beta", "en": "Beta"}, is_correct=True,
            ),
        ]
    )
    db.session.flush()
    return course


def _issue(course):
    trainee = t.create_trainee(
        phone="+447700009000", full_name="Study User", role_code="operator"
    )
    return t.enrol(trainee=trainee, course=course, base_url="https://qms.test")


def test_start_redirects_into_study_when_modules_complete(app, client):
    """Trainee with module_progress >= len(modules) is sent to /study."""
    with app.app_context():
        course = _seed_course()
        enrolment = _issue(course)
        # Pre-advance past the only module so /start branches into study.
        enrolment.module_progress = len(enrolment.course_version.modules)
        db.session.commit()
        token = enrolment.magic_token

    resp = client.post(f"/training/take/{token}/start", follow_redirects=False)
    assert resp.status_code == 302
    assert "/study" in resp.headers["Location"]


def test_next_module_redirects_to_study_after_last_module(app, client):
    with app.app_context():
        course = _seed_course()
        enrolment = _issue(course)
        db.session.commit()
        token = enrolment.magic_token

    client.post(f"/training/take/{token}/start", follow_redirects=False)
    resp = client.post(f"/training/take/{token}/next", follow_redirects=False)
    assert resp.status_code == 302
    assert "/study" in resp.headers["Location"]


def test_study_renders_all_questions_with_correct_marking(app, client):
    with app.app_context():
        course = _seed_course()
        enrolment = _issue(course)
        db.session.commit()
        token = enrolment.magic_token

    resp = client.get(f"/training/take/{token}/study")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Both prompts present (default lang is en in tests).
    assert "First question?" in body
    assert "Second question?" in body

    # Every option label shows up.
    for label in ("Yes-EN", "No-EN", "Alpha", "Beta"):
        assert label in body

    # The is_correct flag drives a CSS hook the front-end uses to mark
    # the option visually. Both classes appear (correct + wrong).
    assert "flashcard-opt-correct" in body
    assert "flashcard-opt-wrong" in body


def test_study_shows_skip_to_exam_link(app, client):
    """The skip button is a plain <a> so it works without JS."""
    with app.app_context():
        course = _seed_course()
        enrolment = _issue(course)
        db.session.commit()
        token = enrolment.magic_token

    resp = client.get(f"/training/take/{token}/study")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Link target points at /exam for this token.
    assert f"/training/take/{token}/exam" in body


def test_submitted_enrolment_skipping_to_study_is_bounced_to_result(app, client):

    with app.app_context():
        course = _seed_course()
        enrolment = _issue(course)
        enrolment.status = EnrolmentStatus.SUBMITTED.value
        db.session.commit()
        token = enrolment.magic_token

    resp = client.get(f"/training/take/{token}/study", follow_redirects=False)
    assert resp.status_code == 302
    assert "/result" in resp.headers["Location"]


def test_invalid_token_on_study_returns_expired_page(client):
    resp = client.get("/training/take/garbage.token.value/study")
    assert resp.status_code == 410
