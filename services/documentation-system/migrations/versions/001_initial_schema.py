"""initial documentation-system schema

Revision ID: 001
Revises:
Create Date: 2026-07-01
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
        "sources",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column(
            "url_patterns",
            ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("ARRAY[]::text[]"),
        ),
        sa.Column("requires_auth", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("has_api", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "content_fetch_enabled", sa.Boolean, nullable=False, server_default=sa.text("false")
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
    )
    op.create_table(
        "docs",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("url_normalized", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column(
            "source_id",
            sa.Text,
            sa.ForeignKey("sources.id"),
            nullable=False,
            server_default=sa.text("'web'"),
        ),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("owning_team_id", UUID(as_uuid=True), nullable=True),
        sa.Column("owning_team_label", sa.Text, nullable=True),
        sa.Column("owning_person_id", UUID(as_uuid=True), nullable=True),
        sa.Column("owning_person_label", sa.Text, nullable=True),
        sa.Column("content_snapshot", sa.Text, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_docs_url_normalized", "docs", ["url_normalized"])
    op.create_index("ix_docs_owning_team", "docs", ["owning_team_id"])
    op.create_index("ix_docs_owning_person", "docs", ["owning_person_id"])
    op.create_index("ix_docs_source", "docs", ["source_id"])
    op.create_table(
        "doc_tags",
        sa.Column(
            "doc_id",
            UUID(as_uuid=True),
            sa.ForeignKey("docs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tag", sa.Text, nullable=False),
        sa.UniqueConstraint("doc_id", "tag", name="uq_doc_tags_doc_tag"),
    )
    op.create_index("ix_doc_tags_tag", "doc_tags", ["tag"])
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
    op.drop_index("ix_doc_tags_tag", table_name="doc_tags")
    op.drop_table("doc_tags")
    for ix in (
        "ix_docs_source",
        "ix_docs_owning_person",
        "ix_docs_owning_team",
        "ix_docs_url_normalized",
    ):
        op.drop_index(ix, table_name="docs")
    op.drop_table("docs")
    op.drop_table("sources")
