"""Record Google roster evidence for individual access.

Revision ID: 012
Revises: 011
Create Date: 2026-11-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "team_google_groups",
        sa.Column("rejected_additions", ARRAY(sa.Text()), nullable=False, server_default="{}"),
    )
    op.add_column("team_google_groups", sa.Column("roster_checked_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("team_google_groups", "roster_checked_at")
    op.drop_column("team_google_groups", "rejected_additions")
