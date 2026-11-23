"""Managed GitHub team identities.

Revision ID: 012
Revises: 011
Create Date: 2026-11-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "team_github_mappings",
        sa.Column("team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id"), primary_key=True),
        sa.Column("organization_id", sa.Text, nullable=False),
        sa.Column("github_team_id", sa.Text),
        sa.Column("slug", sa.Text),
        sa.Column("name", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.UniqueConstraint("organization_id", "github_team_id", name="uq_github_team_identity"),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'inactive')", name="ck_github_mapping_status"
        ),
    )


def downgrade():
    op.drop_table("team_github_mappings")
