"""Module + question authoring routes and markdown rendering."""

from __future__ import annotations

from app.extensions import db
from app.models import (
    QuestionKind,
    TrainingAnswerOption,
    TrainingCourse,
    TrainingModule,
    TrainingQuestion,
)
from app.services import training as t
from app.services.markdown import render as md_render


# ─── Markdown service ──────────────────────────────────────────────


def test_markdown_renders_basics(app):
    with app.app_context():
        out = str(md_render("## Heading\n\n**bold** and *italic*\n\n- a\n- b"))
    assert "<h2>Heading</h2>" in out
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert "<ul>" in out and "<li>a</li>" in out


def test_markdown_strips_inline_html(app):
    """Bleach allow-list must drop <script>, <iframe>, etc.

    With strip=True, the tag is removed but its text content survives
    as plain text — that's the desired behavior (literal text inside a
    <p> can't execute), so we just assert no live tag remains.
    """
    with app.app_context():
        evil = "Hello <script>alert('xss')</script> world"
        out = str(md_render(evil))
    assert "<script>" not in out
    assert "</script>" not in out
    assert "<iframe>" not in str(md_render("<iframe src='evil'></iframe>"))
    assert "Hello" in out and "world" in out


def test_markdown_keeps_safe_links(app):
    with app.app_context():
        out = str(md_render("See [docs](https://example.com)"))
    assert '<a href="https://example.com">docs</a>' in out


def test_markdown_drops_javascript_links(app):
    with app.app_context():
        out = str(md_render("[click](javascript:alert(1))"))
    # bleach strips disallowed protocols — link tag remains but href dropped
    assert "javascript:" not in out


def test_markdown_empty_returns_empty(app):
    with app.app_context():
        assert str(md_render("")) == ""
        assert str(md_render(None)) == ""


# ─── Demo course is seeded ─────────────────────────────────────────


def test_demo_course_seeded_by_conftest(app):
    """conftest.seed_initial now provisions HACCP-REFRESHER."""
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        assert course is not None
        v = course.active_version
        assert v is not None
        assert len(v.modules) == 3
        assert len(v.questions) == 5
        # Every question has at least one correct option.
        for q in v.questions:
            assert any(o.is_correct for o in q.options)


# ─── Module CRUD through admin routes ──────────────────────────────


def _login_admin(client):
    return client.post(
        "/auth/login",
        data={"email": "admin@test", "password": "Admin123!"},
        follow_redirects=False,
    )


def test_module_create_route(app, client):
    _login_admin(client)
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        course_id = course.id
        before = len(course.active_version.modules)

    resp = client.post(
        f"/admin/training/courses/{course_id}/modules/new",
        data={
            "title_pl": "Test moduł",
            "title_en": "Test module",
            "body_pl": "## Treść\n\nLorem ipsum",
            "body_en": "## Content\n\nLorem ipsum",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        assert len(course.active_version.modules) == before + 1
        latest = max(course.active_version.modules, key=lambda m: m.order_index)
        assert latest.title["en"] == "Test module"


def test_module_edit_route(app, client):
    _login_admin(client)
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        mod = course.active_version.modules[0]
        course_id = course.id
        mod_id = mod.id

    resp = client.post(
        f"/admin/training/courses/{course_id}/modules/{mod_id}",
        data={
            "title_pl": "PL nowy",
            "title_en": "EN new",
            "body_pl": "treść",
            "body_en": "body",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        mod = db.session.get(TrainingModule, mod_id)
        assert mod.title["en"] == "EN new"


def test_module_delete_route(app, client):
    _login_admin(client)
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        mod_id = course.active_version.modules[-1].id
        course_id = course.id
        before = len(course.active_version.modules)

    resp = client.post(
        f"/admin/training/courses/{course_id}/modules/{mod_id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        assert len(course.active_version.modules) == before - 1


# ─── Question CRUD through admin routes ────────────────────────────


def test_question_create_route(app, client):
    _login_admin(client)
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        course_id = course.id
        before = len(course.active_version.questions)

    resp = client.post(
        f"/admin/training/courses/{course_id}/questions/new",
        data={
            "prompt_pl": "Pytanie?",
            "prompt_en": "Question?",
            "kind": QuestionKind.SINGLE_CHOICE.value,
            "opt1_pl": "Tak",
            "opt1_en": "Yes",
            "opt1_correct": "y",
            "opt2_pl": "Nie",
            "opt2_en": "No",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        assert len(course.active_version.questions) == before + 1
        latest = max(course.active_version.questions, key=lambda q: q.order_index)
        assert latest.prompt["en"] == "Question?"
        assert len(latest.options) == 2
        assert any(o.is_correct for o in latest.options)


def test_question_create_rejects_no_correct(app, client):
    _login_admin(client)
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        course_id = course.id

    # All options blank-correct → form re-renders with a flash, no question created
    resp = client.post(
        f"/admin/training/courses/{course_id}/questions/new",
        data={
            "prompt_pl": "P?",
            "prompt_en": "Q?",
            "kind": QuestionKind.SINGLE_CHOICE.value,
            "opt1_pl": "A",
            "opt1_en": "A",
            "opt2_pl": "B",
            "opt2_en": "B",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200  # re-render with error flash
    assert b"Mark at least one" in resp.data


def test_question_create_rejects_one_option(app, client):
    _login_admin(client)
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        course_id = course.id

    resp = client.post(
        f"/admin/training/courses/{course_id}/questions/new",
        data={
            "prompt_pl": "P?",
            "prompt_en": "Q?",
            "kind": QuestionKind.SINGLE_CHOICE.value,
            "opt1_pl": "Only",
            "opt1_en": "Only",
            "opt1_correct": "y",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"At least two" in resp.data


def test_question_edit_replaces_options(app, client):
    _login_admin(client)
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        question = course.active_version.questions[0]
        course_id = course.id
        question_id = question.id

    resp = client.post(
        f"/admin/training/courses/{course_id}/questions/{question_id}",
        data={
            "prompt_pl": "Nowe?",
            "prompt_en": "New?",
            "kind": QuestionKind.SINGLE_CHOICE.value,
            "opt1_pl": "X",
            "opt1_en": "X",
            "opt1_correct": "y",
            "opt2_pl": "Y",
            "opt2_en": "Y",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        q = db.session.get(TrainingQuestion, question_id)
        assert q.prompt["en"] == "New?"
        assert len(q.options) == 2
        labels = {o.label["en"] for o in q.options}
        assert labels == {"X", "Y"}


def test_module_renders_markdown_in_trainee_flow(app, client):
    """The trainee module page should render markdown server-side."""
    with app.app_context():
        course = TrainingCourse.query.filter_by(code="HACCP-REFRESHER").first()
        trainee = t.create_trainee(
            phone="+447700055555",
            full_name="Md Reader",
            role_code="operator",
        )
        enrolment = t.enrol(
            trainee=trainee, course=course, base_url="https://qms.test"
        )
        db.session.commit()
        token = enrolment.magic_token

    client.post(f"/training/take/{token}/start")
    resp = client.get(f"/training/take/{token}/module")
    assert resp.status_code == 200
    # The seed's module 0 has "## Czym jest HACCP?" / "## What is HACCP?"
    body = resp.data.decode("utf-8")
    assert "<h2>What is HACCP?</h2>" in body
    assert "<strong>" in body  # **personally own** rendered
