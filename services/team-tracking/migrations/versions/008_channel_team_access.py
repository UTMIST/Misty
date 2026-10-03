"""channel_team_access

Revision ID: 008
Revises: 007
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_team_access",
        sa.Column("guild_id", sa.Text, primary_key=True),
        sa.Column("channel_id", sa.Text, primary_key=True),
        sa.Column("team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id"), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("channel_team_access")
