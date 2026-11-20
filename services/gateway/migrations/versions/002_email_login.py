"""Durable email login challenges and abuse limits.

Revision ID: 002
Revises: 001
Create Date: 2026-10-26
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_challenges",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("person_id", UUID(as_uuid=True), nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("redeemed", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_login_challenges_expires_at", "login_challenges", ["expires_at"])
    op.create_index("ix_login_challenges_owner_email", "login_challenges", ["owner", "email"])
    op.create_table(
        "login_limits",
        sa.Column("bucket", sa.Text, primary_key=True),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_login_limits_expires_at", "login_limits", ["expires_at"])


def downgrade() -> None:
    op.drop_table("login_limits")
    op.drop_table("login_challenges")
