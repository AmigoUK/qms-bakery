"""TrainingEnrolment — three reminder timestamps.

Revision ID: f1a2b3c4d5e7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-05

Adds three nullable DateTime columns to `training_enrolments` so the
training-scheduler can track which reminders it has already issued
for each open enrolment:

- `reminder_3d_sent_at`    — first nudge, 3 days after issue
- `reminder_7d_sent_at`    — softer reminder, 7 days after issue
- `reminder_final_sent_at` — final warning on the day the link expires
                             ("not cleared for work today" message)

NULL means the scheduler has not yet sent that kind. No backfill —
existing enrolments simply get a one-shot reminder cycle going
forward, which is what an operator would expect after deploying
this feature.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "f1a2b3c4d5e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("training_enrolments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("reminder_3d_sent_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("reminder_7d_sent_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("reminder_final_sent_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("training_enrolments", schema=None) as batch_op:
        batch_op.drop_column("reminder_final_sent_at")
        batch_op.drop_column("reminder_7d_sent_at")
        batch_op.drop_column("reminder_3d_sent_at")
