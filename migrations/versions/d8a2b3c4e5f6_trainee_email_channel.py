"""Trainee — email + notification_channel.

Revision ID: d8a2b3c4e5f6
Revises: c9e1a2b3d4f5
Create Date: 2026-05-01

Adds two columns to `trainees`:

- `email` — VARCHAR(255), NULLABLE. New trainees enrolled via the
  admin form must supply it; existing rows stay NULL until manually
  filled, which is fine because the channel default below keeps them
  on SMS-only.
- `notification_channel` — VARCHAR(10), NOT NULL, default 'sms'.
  Backfilled to 'sms' for every existing row so the magic-link flow
  keeps using ClickSend exactly as before. Admin can flip to
  'email' or 'both' on a per-trainee basis afterwards.

No data migration beyond the default backfill — there's nothing
sensitive to compute.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "d8a2b3c4e5f6"
down_revision = "c9e1a2b3d4f5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("trainees", schema=None) as batch_op:
        batch_op.add_column(sa.Column("email", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "notification_channel",
                sa.String(length=10),
                nullable=False,
                server_default="sms",
            )
        )


def downgrade():
    with op.batch_alter_table("trainees", schema=None) as batch_op:
        batch_op.drop_column("notification_channel")
        batch_op.drop_column("email")
