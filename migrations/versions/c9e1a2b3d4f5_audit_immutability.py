"""Audit-log immutability trigger (Postgres only).

Revision ID: c9e1a2b3d4f5
Revises: 57a060b53c8f
Create Date: 2026-04-30

Adds a BEFORE UPDATE OR DELETE trigger on `audit_log` that raises
EXCEPTION unless the connection has set the `qms.audit_redact_ok`
session GUC to `'1'`. The redact-flow in app/services/gdpr.py opts in
explicitly via SET LOCAL; everything else is denied at the DB level.

SQLite has no triggers and is single-writer anyway — skipped there.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "c9e1a2b3d4f5"
down_revision = "57a060b53c8f"
branch_labels = None
depends_on = None


_FN = """
CREATE OR REPLACE FUNCTION qms_audit_log_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Only the documented redact path may mutate; it sets
    -- SET LOCAL qms.audit_redact_ok = '1' for the duration of the
    -- transaction. Everything else is rejected here.
    IF current_setting('qms.audit_redact_ok', true) = '1' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    RAISE EXCEPTION
        'audit_log is append-only — % blocked. Use the gdpr-redact CLI '
        'for legitimate redaction.', TG_OP;
END;
$$;
"""

_TRG = """
DROP TRIGGER IF EXISTS audit_log_immutable_trg ON audit_log;
CREATE TRIGGER audit_log_immutable_trg
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION qms_audit_log_immutable();
"""

_DROP = """
DROP TRIGGER IF EXISTS audit_log_immutable_trg ON audit_log;
DROP FUNCTION IF EXISTS qms_audit_log_immutable();
"""


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(_FN)
    op.execute(_TRG)


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(_DROP)
