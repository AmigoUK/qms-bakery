"""Audit chain integrity tests."""

from __future__ import annotations

from app.extensions import db
from app.models.audit import AuditLog
from app.services import audit


def test_first_record_has_genesis_prev(app):
    with app.test_request_context("/"):
        entry = audit.record(entity_type="test", action="create", entity_id="x")
        db.session.commit()
    assert entry.prev_checksum == "0" * 64
    assert entry.checksum != entry.prev_checksum
    assert len(entry.checksum) == 64


def test_chain_links_consecutive_records(app):
    with app.test_request_context("/"):
        a = audit.record(entity_type="test", action="create", entity_id="1")
        b = audit.record(entity_type="test", action="update", entity_id="1")
        c = audit.record(entity_type="test", action="delete", entity_id="1")
        db.session.commit()

    assert b.prev_checksum == a.checksum
    assert c.prev_checksum == b.checksum
    ok, broken = audit.verify_chain()
    assert ok and broken is None


def test_tampering_breaks_chain(app):
    with app.test_request_context("/"):
        audit.record(entity_type="test", action="a", entity_id="1")
        audit.record(entity_type="test", action="b", entity_id="1")
        third = audit.record(entity_type="test", action="c", entity_id="1")
        db.session.commit()

        # Simulate tamper: someone changes the action of the middle record.
        target = AuditLog.query.filter_by(action="b").first()
        target.action = "MALICIOUS"
        db.session.commit()

        ok, broken = audit.verify_chain()
        assert ok is False
        assert broken == target.id

        # Note: `third` is unchanged, but its prev_checksum is now invalid
        # because the middle record's checksum (when recomputed) would differ.
        assert third is not None
