"""Trigger form-builder: create + edit via /admin/triggers/new and /edit.

Covers the structured-fields flow that replaces raw JSON editing of the
`condition` column. We don't test rendering — just the round-trip from
form fields to a Trigger row + responder associations + audit entry.
"""

from __future__ import annotations

from app.extensions import db
from app.models import Trigger
from app.models.audit import AuditLog
from app.models.triggers import Responder, ResponderType, trigger_responders


def _make_responder(code: str, type_: ResponderType) -> Responder:
    r = Responder(
        code=code,
        name={"en": code, "pl": code},
        type=type_.value,
        config={},
    )
    db.session.add(r)
    db.session.flush()
    return r


def _new_trigger_form_data(**overrides) -> dict:
    base = {
        "code": "OVEN1_HIGH",
        "name_en": "Oven 1 high",
        "name_pl": "Piec 1 wysoki",
        "scope": "line:LINE_A",
        "metric": "temperature",
        "operator": ">",
        "value": "230.0",
        "duration_seconds": "0",
        "severity": "high",
        "is_active": "y",
        "submit": "Save",
    }
    base.update(overrides)
    return base


def test_get_new_form_renders(app, client, login_admin):
    login_admin()
    resp = client.get("/admin/triggers/new")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Curated metrics + scope dropdown populated
    assert "temperature" in body
    assert "line:LINE_A" in body


def test_post_new_creates_trigger_and_audits(app, client, login_admin):
    login_admin()
    with app.app_context():
        notify = _make_responder("NOTIFY_ALL", ResponderType.NOTIFY_IN_APP)
        notify_id = notify.id
        db.session.commit()

    resp = client.post(
        "/admin/triggers/new",
        data=_new_trigger_form_data(
            duration_seconds="30", responder_ids=[notify_id]
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        t = Trigger.query.filter_by(code="OVEN1_HIGH").first()
        assert t is not None
        assert t.scope == "line:LINE_A"
        assert t.condition == {
            "metric": "temperature",
            "operator": ">",
            "value": 230.0,
            "duration_seconds": 30,
        }
        assert t.severity == "high"
        assert t.is_active is True
        # Responder M:N row created with order_index=0
        rows = db.session.execute(
            trigger_responders.select().where(
                trigger_responders.c.trigger_id == t.id
            )
        ).all()
        assert len(rows) == 1
        # audit row recorded
        assert AuditLog.query.filter_by(
            entity_type="trigger", entity_id=t.id, action="create"
        ).first() is not None


def test_post_new_with_other_metric_uses_freetext(app, client, login_admin):
    login_admin()
    resp = client.post(
        "/admin/triggers/new",
        data=_new_trigger_form_data(
            code="VIBRATION_HIGH",
            metric="__other__",
            metric_other="vibration_rms",
            value="4.5",
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        t = Trigger.query.filter_by(code="VIBRATION_HIGH").first()
        assert t.condition["metric"] == "vibration_rms"


def test_post_new_other_metric_without_text_rejected(app, client, login_admin):
    login_admin()
    resp = client.post(
        "/admin/triggers/new",
        data=_new_trigger_form_data(
            code="WILL_NOT_SAVE",
            metric="__other__",
            metric_other="",
        ),
        follow_redirects=False,
    )
    # Re-renders form rather than redirecting
    assert resp.status_code == 200
    with app.app_context():
        assert Trigger.query.filter_by(code="WILL_NOT_SAVE").first() is None


def test_post_new_duplicate_code_rejected(app, client, login_admin):
    login_admin()
    # OVEN1_OVERHEAT already in seeds
    resp = client.post(
        "/admin/triggers/new",
        data=_new_trigger_form_data(code="OVEN1_OVERHEAT"),
        follow_redirects=False,
    )
    assert resp.status_code == 200  # form re-renders
    with app.app_context():
        # Still only one row with that code
        assert Trigger.query.filter_by(code="OVEN1_OVERHEAT").count() == 1


def test_get_edit_form_prefills_from_existing(app, client, login_admin):
    login_admin()
    with app.app_context():
        t = Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()
        tid = t.id
    resp = client.get(f"/admin/triggers/{tid}/edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "OVEN1_OVERHEAT" in body
    assert 'value="220' in body or "220" in body  # threshold pre-filled


def test_post_edit_updates_condition_and_audits(app, client, login_admin):
    login_admin()
    with app.app_context():
        t = Trigger.query.filter_by(code="OVEN1_OVERHEAT").first()
        tid = t.id

    resp = client.post(
        f"/admin/triggers/{tid}/edit",
        data=_new_trigger_form_data(
            code="OVEN1_OVERHEAT",
            value="225.0",
            duration_seconds="60",
            severity="critical",
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        t = db.session.get(Trigger, tid)
        assert t.condition["value"] == 225.0
        assert t.condition["duration_seconds"] == 60
        assert t.severity == "critical"
        assert AuditLog.query.filter_by(
            entity_type="trigger", entity_id=tid, action="update"
        ).first() is not None


def test_duration_zero_omits_key(app, client, login_admin):
    """A `duration_seconds=0` field must NOT serialise into the JSON,
    so the trigger engine's `int(condition.get("duration_seconds") or 0)`
    logic stays simple."""
    login_admin()
    resp = client.post(
        "/admin/triggers/new",
        data=_new_trigger_form_data(code="NO_DURATION", duration_seconds="0"),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        t = Trigger.query.filter_by(code="NO_DURATION").first()
        assert "duration_seconds" not in t.condition


def test_responder_ordering_persisted(app, client, login_admin):
    """Selection order in the multi-select must become order_index in
    the M:N table — the trigger engine dispatches in that order."""
    login_admin()
    with app.app_context():
        a = _make_responder("R_FIRST", ResponderType.NOTIFY_IN_APP)
        b = _make_responder("R_SECOND", ResponderType.NOTIFY_IN_APP)
        a_id, b_id = a.id, b.id
        db.session.commit()

    resp = client.post(
        "/admin/triggers/new",
        data=_new_trigger_form_data(
            code="ORDERED",
            responder_ids=[b_id, a_id],  # B first, A second
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with app.app_context():
        t = Trigger.query.filter_by(code="ORDERED").first()
        rows = db.session.execute(
            trigger_responders.select()
            .where(trigger_responders.c.trigger_id == t.id)
            .order_by(trigger_responders.c.order_index)
        ).all()
        assert [r.responder_id for r in rows] == [b_id, a_id]


def test_form_requires_triggers_define_permission(app, client):
    """Anonymous → bounced to login."""
    resp = client.get("/admin/triggers/new")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
