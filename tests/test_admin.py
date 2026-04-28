"""Admin panel tests - permission gating, user CRUD, trigger toggle, audit view."""

from __future__ import annotations

import pytest

from app.auth import hash_password
from app.extensions import db
from app.models import Role, Trigger, User


def _login(client, email="admin@test", password="Admin123!"):
    return client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


@pytest.fixture()
def operator_user(app):
    op_role = Role.query.filter_by(code="operator").first()
    user = User(
        email="op_admin@test",
        password_hash=hash_password("Op123456!"),
        full_name="Op",
        language="en",
        role_id=op_role.id,
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_admin_index_requires_permission(client, operator_user):
    _login(client, "op_admin@test", "Op123456!")
    resp = client.get("/admin/")
    assert resp.status_code == 403


def test_admin_index_for_admin(client, login_admin):
    login_admin()
    resp = client.get("/admin/")
    assert resp.status_code == 200
    # KPI labels rendered.
    assert b"Administration" in resp.data or b"Administracja" in resp.data


def test_users_list(client, login_admin):
    login_admin()
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    assert b"admin@test" in resp.data


def test_create_user(client, login_admin, app):
    login_admin()
    resp = client.post(
        "/admin/users/new",
        data={
            "email": "newqa@test",
            "full_name": "New QA",
            "role_code": "qa",
            "language": "pl",
            "password": "Strong123!",
            "is_active": "y",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    with app.app_context():
        u = User.query.filter_by(email="newqa@test").first()
        assert u is not None
        assert u.role.code == "qa"
        assert u.language == "pl"


def test_create_user_short_password_rejected(client, login_admin):
    login_admin()
    resp = client.post(
        "/admin/users/new",
        data={
            "email": "weakpass@test",
            "full_name": "Weak",
            "role_code": "qa",
            "language": "en",
            "password": "short",
            "is_active": "y",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert User.query.filter_by(email="weakpass@test").first() is None


def test_trigger_toggle(client, login_admin, app):
    login_admin()
    with app.app_context():
        t = Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()
        original = t.is_active
        trigger_id = t.id

    resp = client.post(f"/admin/triggers/{trigger_id}/toggle", follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        t = db.session.get(Trigger, trigger_id)
        assert t.is_active != original


def test_audit_view_with_chain_status(client, login_admin):
    login_admin()
    resp = client.get("/admin/audit")
    assert resp.status_code == 200
    # Either of the i18n strings must appear.
    body = resp.data
    assert b"Chain integrity OK" in body or b"chain_ok" in body or b"sp\xc3\xb3jny" in body


def test_admin_audit_blocked_for_operator(client, operator_user):
    _login(client, "op_admin@test", "Op123456!")
    resp = client.get("/admin/audit")
    assert resp.status_code == 403
