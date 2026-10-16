"""team_google_groups

Revision ID: 009
Revises: 008
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_google_groups",
        sa.Column("team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id"), primary_key=True),
        sa.Column("group_email", sa.Text, nullable=False),
        sa.Column("group_name", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.CheckConstraint(
            "status IN ('synced', 'needs_external_members', 'failed')",
            name="ck_team_google_groups_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("team_google_groups")
