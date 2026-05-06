"""Trainee — employee_number (HR-stable identifier).

Revision ID: e1f2a3b4c5d6
Revises: d8a2b3c4e5f6
Create Date: 2026-05-01

Adds the `employee_number` column to `trainees`. NULLABLE so existing
rows survive the migration; UNIQUE so the operator can't accidentally
duplicate. The admin form requires it on new records and audits any
later edits, so the data eventually reaches "every active trainee
has one" without forcing a hard backfill on day zero.

Phone stays around as a contact channel but is no longer the
identity-of-record (phone numbers move between people).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e1f2a3b4c5d6"
down_revision = "d8a2b3c4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("trainees", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("employee_number", sa.String(length=32), nullable=True)
        )
        batch_op.create_index(
            "ix_trainees_employee_number",
            ["employee_number"],
            unique=True,
        )


def downgrade():
    with op.batch_alter_table("trainees", schema=None) as batch_op:
        batch_op.drop_index("ix_trainees_employee_number")
        batch_op.drop_column("employee_number")
