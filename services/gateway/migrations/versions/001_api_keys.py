"""api_keys table

Revision ID: 001
Revises:
Create Date: 2026-07-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("prefix", sa.Text, nullable=False, unique=True),
        sa.Column("key_hash", sa.Text, nullable=False),
        sa.Column(
            "scopes", ARRAY(sa.Text), nullable=False, server_default=sa.text("ARRAY[]::text[]")
        ),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("true")),
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
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("api_keys")
