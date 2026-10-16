"""persist unconfirmed Google membership changes

Revision ID: 010
Revises: 009
Create Date: 2026-10-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "team_google_groups",
        sa.Column("pending_changes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    # Older code cannot account for a write that might still complete in Google.
    # Match migration 006's refusal to discard live state during rollback.
    conn = op.get_bind()
    if conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM team_google_groups WHERE pending_changes <> '[]'::jsonb)"
        )
    ).scalar():
        raise RuntimeError("Resolve pending Google membership changes before downgrading")
    op.drop_column("team_google_groups", "pending_changes")
