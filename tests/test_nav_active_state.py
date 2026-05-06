"""Topbar active-state and section-classifier tests.

Two contracts under test:

  1. The `_classify_endpoint()` helper at module level classifies
     endpoints into one of three buckets: "operations" | "compliance"
     | "system" (or None for un-classified endpoints like
     dashboard.index or auth.*).

  2. The `nav_link` macro in `_macros.html` adds `aria-current="page"`
     to a link whose endpoint matches the current request endpoint.
     The `nav_section` wrapper highlights the section containing the
     active page via the `nav-section-current` class.

These together drive the topbar active-state highlight.
"""

from __future__ import annotations


def test_endpoint_classifier_buckets():
    """The longest-prefix matcher classifies each endpoint into the
    right section. The key edge case: admin.audit_index lives in
    admin_bp but should match 'compliance', not 'system'."""
    from app import _classify_endpoint

    assert _classify_endpoint("admin.audit_index") == "compliance"
    assert _classify_endpoint("admin.users_index") == "system"
    assert _classify_endpoint("admin.triggers_index") == "system"
    assert _classify_endpoint("admin_training.dashboard") == "compliance"
    assert _classify_endpoint("admin_training.trainees_index") == "compliance"
    assert _classify_endpoint("reports.haccp_monthly") == "compliance"
    assert _classify_endpoint("tickets.index") == "operations"
    assert _classify_endpoint("haccp.index") == "operations"
    assert _classify_endpoint("salsa.index") == "operations"
    assert _classify_endpoint("dashboard.index") is None
    assert _classify_endpoint("auth.login") is None
    assert _classify_endpoint(None) is None
    assert _classify_endpoint("") is None


def test_topbar_renders_three_sections_for_admin(client, login_admin):
    """An admin (with all permissions) sees the three section
    dropdowns in the drawer."""
    login_admin()
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.data.decode()
    # Each section <details> wraps its summary label.
    assert "nav.section.operations" in body or "Operations" in body or "Operacje" in body
    assert "nav.section.compliance" in body or "Compliance" in body or "Zgod" in body
    assert "nav.section.system" in body or "System" in body
    # Class hooks are present
    assert 'class="nav-drawer"' in body or 'nav-drawer"' in body
    assert "nav-section" in body


def test_active_section_class_on_tickets_page(client, login_admin):
    """Visiting /tickets must mark the Operations section current,
    not Compliance or System."""
    login_admin()
    resp = client.get("/tickets", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.data.decode()

    # Find each section's <details ...> opening tag and check whether
    # nav-section-current is on it.
    import re

    section_tags = re.findall(
        r'<details class="nav-section[^"]*"', body
    )
    # We expect three sections at most; one of them should carry the
    # current marker.
    current = [t for t in section_tags if "nav-section-current" in t]
    assert len(current) == 1, (
        f"Expected exactly one section flagged current; got {current!r} "
        f"out of {section_tags!r}"
    )
    # The current one is the first (Operations). With the templates'
    # rendering order (Operations → Compliance → System), the index
    # tells us which.
    assert section_tags.index(current[0]) == 0


def test_active_section_class_on_users_page(client, login_admin):
    """Visiting /admin/users must mark the System section current."""
    login_admin()
    resp = client.get("/admin/users", follow_redirects=True)
    assert resp.status_code == 200
    body = resp.data.decode()

    import re

    section_tags = re.findall(
        r'<details class="nav-section[^"]*"', body
    )
    current = [t for t in section_tags if "nav-section-current" in t]
    assert len(current) == 1
    # System is the last of the three sections.
    assert section_tags.index(current[0]) == len(section_tags) - 1
