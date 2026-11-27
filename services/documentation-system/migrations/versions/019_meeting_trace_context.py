"""Retain the accepted recording span for asynchronous save recovery.

Revision ID: 019
Revises: 018
"""

from alembic import op
import sqlalchemy as sa

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meeting_records", sa.Column("accept_traceparent", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("meeting_records", "accept_traceparent")
