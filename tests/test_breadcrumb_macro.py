"""Breadcrumb macro renders an accessible <ol> trail.

Contract:
- Wraps in <nav aria-label="Breadcrumb"> with `.breadcrumb` class.
- Earlier crumbs are tuples (label, url) → <li><a>label</a></li>.
- Last crumb is either a string or a tuple; rendered without a link
  and marked aria-current="page".
"""

from __future__ import annotations


def _render(app, crumbs):
    """Helper: render the breadcrumb macro standalone."""
    with app.test_request_context("/"):
        from flask import render_template_string

        return render_template_string(
            '{% from "_macros.html" import breadcrumb %}'
            "{{ breadcrumb(crumbs) }}",
            crumbs=crumbs,
        )


def test_two_level_breadcrumb_links_first_marks_last_current(app):
    out = _render(app, [("Admin", "/admin"), "Users"])
    assert 'aria-label="Breadcrumb"' in out
    assert 'class="breadcrumb"' in out
    assert '<a href="/admin">Admin</a>' in out
    assert 'aria-current="page"' in out
    assert "Users" in out
    # Sanity: the last segment is NOT inside an <a>
    last_idx = out.find('aria-current="page"')
    a_after = out.find("<a", last_idx)
    li_close = out.find("</li>", last_idx)
    assert a_after == -1 or a_after > li_close


def test_three_level_breadcrumb(app):
    out = _render(app, [("Admin", "/admin"), ("Courses", "/admin/training/courses"), "Modules"])
    # Both intermediate links present
    assert '<a href="/admin">Admin</a>' in out
    assert '<a href="/admin/training/courses">Courses</a>' in out
    # Final segment is the current page
    assert "Modules" in out
    assert out.count('aria-current="page"') == 1


def test_courses_list_renders_breadcrumb(client, login_admin):
    """End-to-end: the courses list page now ships a breadcrumb."""
    login_admin()
    resp = client.get("/admin/training/courses", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'aria-label="Breadcrumb"' in body
    # The breadcrumb's last segment carries the page label.
    assert 'aria-current="page"' in body
