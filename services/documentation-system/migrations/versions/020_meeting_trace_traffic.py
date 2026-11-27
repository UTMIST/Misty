"""Keep the accepted traffic class when a different worker resumes the recording.

Revision ID: 020
Revises: 019
"""

from alembic import op
import sqlalchemy as sa

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meeting_records", sa.Column("accept_traffic", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("meeting_records", "accept_traffic")
