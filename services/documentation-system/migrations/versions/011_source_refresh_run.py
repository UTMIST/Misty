"""Persist ownership of the source refresh runner across processes and restarts.

Revision ID: 011
Revises: 010
Create Date: 2026-10-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_refresh_run",
        sa.Column("singleton", sa.Boolean(), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton", name="ck_source_refresh_run_singleton"),
    )


def downgrade() -> None:
    op.drop_table("source_refresh_run")
