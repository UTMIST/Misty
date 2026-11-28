"""Store private weekly transcription vocabulary candidates.

Revision ID: 019
Revises: 018
Create Date: 2026-11-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vocabulary_runs",
        sa.Column("week", sa.Date(), primary_key=True),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("rule_versions", postgresql.JSONB(), nullable=False),
        sa.Column("candidates", postgresql.JSONB(), nullable=False),
        sa.Column("additions", postgresql.JSONB(), nullable=False),
        sa.Column("removals", postgresql.JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vocabulary_runs")
