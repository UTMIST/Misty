"""Remember confirmed canonical Google deletion across cleanup retries.

Revision ID: 014
Revises: 013
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_records", sa.Column("google_deleted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("meeting_records", "google_deleted_at")
