"""Verified GitHub account IDs, separate from unverified legacy identifiers.

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
        "verified_github_identities",
        sa.Column("person_id", UUID(as_uuid=True), sa.ForeignKey("people.id"), primary_key=True),
        sa.Column("github_id", sa.Text, nullable=True, unique=True),
        sa.Column("login", sa.Text, nullable=True),
        sa.Column(
            "verified_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("updated_by", sa.Text, nullable=False),
    )


def downgrade():
    op.drop_table("verified_github_identities")
