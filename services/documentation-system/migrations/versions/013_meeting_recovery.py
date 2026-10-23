"""Remember initial meeting indexing work across failures and restarts.

Revision ID: 013
Revises: 012
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meeting_records",
        sa.Column("index_pending", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    # Existing saved meetings may have content but no initial search index.
    op.execute("UPDATE meeting_records SET index_pending = true WHERE persistence_status = 'saved'")


def downgrade() -> None:
    op.drop_column("meeting_records", "index_pending")
