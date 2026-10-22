"""Fence canonical adoption before issuing Google deletion.

Revision ID: 016
Revises: 015
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_records",
        sa.Column(
            "google_deletions", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")
        ),
    )


def downgrade() -> None:
    op.drop_column("meeting_records", "google_deletions")
