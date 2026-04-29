"""Pipeline configurator: list view + drag-and-drop editor.

The editor saves a JSON blob (`stages_json`) describing the ordered stages.
On save, a NEW Pipeline row is created with `version = max+1, is_active=True`
and the previous version is deactivated. Existing tickets keep their FK to
the old Pipeline/PipelineStage rows.
"""

from __future__ import annotations

import json

from app.extensions import db
from app.models import Pipeline, PipelineStage, ProductionLine
from app.models.audit import AuditLog


def _post_stages(client, line_id, stages):
    return client.post(
        f"/admin/pipelines/{line_id}/edit",
        data={"stages_json": json.dumps(stages), "submit": "Save"},
        follow_redirects=False,
    )


def _baseline_stages():
    return [
        {
            "code": "intake",
            "name_pl": "Przyjęcie",
            "name_en": "Intake",
            "required_role_code": "qa",
            "sla_minutes": 15,
            "is_ccp_checkpoint": False,
        },
        {
            "code": "review",
            "name_pl": "Przegląd",
            "name_en": "Review",
            "required_role_code": "line_manager",
            "sla_minutes": 30,
            "is_ccp_checkpoint": True,
        },
    ]


def test_list_renders_active_pipeline(app, client, login_admin):
    login_admin()
    resp = client.get("/admin/pipelines")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "LINE_A" in body
    # Demo seed creates a v1 pipeline for LINE_A
    assert "v1" in body


def test_get_edit_prefills_existing_stages(app, client, login_admin):
    login_admin()
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        line_id = line.id
    resp = client.get(f"/admin/pipelines/{line_id}/edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Seeded stage codes appear pre-filled
    assert 'value="detection"' in body
    assert 'value="classification"' in body


def test_save_creates_new_version_and_audits(app, client, login_admin):
    login_admin()
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        line_id = line.id
        prev_active = (
            Pipeline.query.filter_by(line_id=line_id, is_active=True)
            .order_by(Pipeline.version.desc())
            .first()
        )
        prev_version = prev_active.version
        prev_id = prev_active.id

    resp = _post_stages(client, line_id, _baseline_stages())
    assert resp.status_code == 302

    with app.app_context():
        # Old version deactivated, kept in DB
        prev = db.session.get(Pipeline, prev_id)
        assert prev.is_active is False
        # New active version created
        new_active = (
            Pipeline.query.filter_by(line_id=line_id, is_active=True)
            .order_by(Pipeline.version.desc())
            .first()
        )
        assert new_active.version == prev_version + 1
        assert new_active.id != prev_id
        # Stages persisted in submitted order
        codes = [s.code for s in new_active.stages]
        assert codes == ["intake", "review"]
        review = new_active.stages[1]
        assert review.is_ccp_checkpoint is True
        assert review.required_role_code == "line_manager"
        assert review.sla_minutes == 30
        # Audit row recorded
        log = AuditLog.query.filter_by(
            entity_type="pipeline", entity_id=new_active.id, action="create_version"
        ).first()
        assert log is not None
        assert log.diff["stage_codes"] == ["intake", "review"]


def test_reordering_persists_order(app, client, login_admin):
    """Submission order maps directly to PipelineStage.order_index."""
    login_admin()
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        line_id = line.id

    stages = list(reversed(_baseline_stages()))
    resp = _post_stages(client, line_id, stages)
    assert resp.status_code == 302

    with app.app_context():
        new_active = (
            Pipeline.query.filter_by(line_id=line_id, is_active=True)
            .order_by(Pipeline.version.desc())
            .first()
        )
        assert [s.code for s in new_active.stages] == ["review", "intake"]


def test_empty_pipeline_rejected(app, client, login_admin):
    login_admin()
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        line_id = line.id
        before_count = Pipeline.query.filter_by(line_id=line_id).count()

    resp = _post_stages(client, line_id, [])
    # Re-renders form rather than redirecting
    assert resp.status_code == 200
    with app.app_context():
        # No new version created
        assert Pipeline.query.filter_by(line_id=line_id).count() == before_count


def test_invalid_code_rejected(app, client, login_admin):
    login_admin()
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        line_id = line.id

    bad = _baseline_stages()
    bad[0]["code"] = "Has Spaces!"
    resp = _post_stages(client, line_id, bad)
    assert resp.status_code == 200
    with app.app_context():
        # Active version unchanged from seed (still detection-classification-...)
        active = (
            Pipeline.query.filter_by(line_id=line_id, is_active=True)
            .order_by(Pipeline.version.desc())
            .first()
        )
        assert active.stages[0].code == "detection"


def test_duplicate_codes_rejected(app, client, login_admin):
    login_admin()
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        line_id = line.id

    dupes = _baseline_stages()
    dupes[1]["code"] = dupes[0]["code"]
    resp = _post_stages(client, line_id, dupes)
    assert resp.status_code == 200


def test_missing_name_rejected(app, client, login_admin):
    login_admin()
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        line_id = line.id

    bad = _baseline_stages()
    bad[0]["name_en"] = ""
    resp = _post_stages(client, line_id, bad)
    assert resp.status_code == 200


def test_unknown_role_rejected(app, client, login_admin):
    login_admin()
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        line_id = line.id

    bad = _baseline_stages()
    bad[0]["required_role_code"] = "ceo"  # not a seeded role
    resp = _post_stages(client, line_id, bad)
    assert resp.status_code == 200


def test_404_for_unknown_line(app, client, login_admin):
    login_admin()
    resp = client.get("/admin/pipelines/00000000-0000-0000-0000-000000000000/edit")
    assert resp.status_code == 404


def test_anonymous_redirected_to_login(app, client):
    resp = client.get("/admin/pipelines")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_old_stage_rows_preserved_after_new_version(app, client, login_admin):
    """Tickets reference PipelineStage by FK, so old stage rows must survive
    the version bump even if their codes don't appear in the new version."""
    login_admin()
    with app.app_context():
        line = ProductionLine.query.filter_by(code="LINE_A").first()
        line_id = line.id
        old_stage_ids = {
            s.id
            for s in PipelineStage.query.join(Pipeline)
            .filter(Pipeline.line_id == line_id)
            .all()
        }

    resp = _post_stages(client, line_id, _baseline_stages())
    assert resp.status_code == 302

    with app.app_context():
        # Every old stage ID still resolvable (no cascade-delete on version flip)
        for sid in old_stage_ids:
            assert db.session.get(PipelineStage, sid) is not None
