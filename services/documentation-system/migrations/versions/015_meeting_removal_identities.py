"""Fence every known Google identity of a removed meeting.

Revision ID: 015
Revises: 014
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_records",
        sa.Column(
            "removed_google_document_ids",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # Reconcile old tombstones even when Google has no surviving tagged files.
    op.execute(
        "UPDATE meeting_records SET next_reconcile_at = CURRENT_TIMESTAMP, "
        "removed_google_document_ids = CASE WHEN google_document_id IS NULL "
        "THEN '{}'::text[] ELSE ARRAY[google_document_id] END WHERE removed_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("meeting_records", "removed_google_document_ids")
