"""Track the title context used to embed document chunks.

Existing chunks remain readable with unknown title provenance (NULL) and
are picked up by the normal stale-index retry operation.

Revision ID: 010
Revises: 009
Create Date: 2026-10-18
"""

from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("doc_chunks", sa.Column("source_title_hash", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_doc_chunks_title_hash", "doc_chunks", "source_title_hash ~ '^[0-9a-f]{64}$'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_doc_chunks_title_hash", "doc_chunks", type_="check")
    op.drop_column("doc_chunks", "source_title_hash")
