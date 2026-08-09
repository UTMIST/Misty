"""doc_content: full extracted document text

Full text lives in its own 1:1 table rather than widening the hot `docs` row,
so list/filter queries stay lean. Absence of a row means "no full content
captured yet". Cascades on doc delete.

Revision ID: 005
Revises: 004
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "doc_content",
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("docs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("doc_content")
