"""Smoke tests for model schema and basic relationships."""

from __future__ import annotations

from app.models import Permission, ProductionLine, Role, User
from app.models.auth import UserRoleEnum


def test_seed_roles_and_permissions(app):
    assert Role.query.count() == len(UserRoleEnum)
    # Sanity: every role code is unique and has at least one permission.
    for role in Role.query.all():
        assert role.permissions, f"Role {role.code} has no permissions"


def test_admin_has_all_permissions(app):
    admin_role = Role.query.filter_by(code="admin").first()
    perm_count = Permission.query.count()
    assert len(admin_role.permissions) == perm_count


def test_operator_cannot_define_ccp(app):
    op = Role.query.filter_by(code="operator").first()
    assert not op.has_permission("ccp.define")
    assert op.has_permission("tickets.create")


def test_user_login_relationship(app):
    user = User.query.filter_by(email="admin@test").first()
    assert user is not None
    assert user.role.code == "admin"
    assert user.has_permission("system.configure")


def test_production_line_seeded(app):
    line = ProductionLine.query.filter_by(code="LINE_A").first()
    assert line is not None
    assert line.is_active
    assert line.pipelines, "Demo line should have a pipeline"
    pipe = line.pipelines[0]
    assert len(pipe.stages) == 6
    # Stages must be ordered.
    indices = [s.order_index for s in pipe.stages]
    assert indices == sorted(indices)
