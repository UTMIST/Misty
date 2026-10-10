"""doc_chunks: document-linked embeddings and keyword-search metadata

The initial /embed profile is text-embedding-3-small at 1536 dimensions.
Exact vector scans suit the estimated 1,000-5,000 chunks (#208); only the
keyword branch needs a search index initially.

Revision ID: 007
Revises: 006
Create Date: 2026-10-09
"""

from alembic import op
from pgvector.sqlalchemy import VECTOR
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.create_table(
        "doc_chunks",
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("docs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("source_content_hash", sa.Text(), nullable=False),
        sa.Column("embedding", VECTOR(1536), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, chunk_text)", persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_doc_chunks_ordinal"),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset "
            "AND char_length(chunk_text) = end_offset - start_offset",
            name="ck_doc_chunks_offsets",
        ),
        sa.CheckConstraint(
            "source_content_hash ~ '^[0-9a-f]{64}$'", name="ck_doc_chunks_content_hash"
        ),
        sa.CheckConstraint("embedding_model ~ '[^[:space:]]'", name="ck_doc_chunks_model"),
        sa.CheckConstraint("dimensions = 1536", name="ck_doc_chunks_dimensions"),
    )
    op.create_index(
        "ix_doc_chunks_search_vector", "doc_chunks", ["search_vector"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_doc_chunks_search_vector", table_name="doc_chunks")
    op.drop_table("doc_chunks")
    # This service owns its database and this extension. No CASCADE: unrelated
    # vector dependencies must cause rollback rather than silently lose data.
    op.execute(sa.text("DROP EXTENSION IF EXISTS vector"))
