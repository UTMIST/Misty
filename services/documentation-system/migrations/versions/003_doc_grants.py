"""doc_grants: per-document visibility grants

Revision ID: 003
Revises: 002
Create Date: 2026-07-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "doc_grants",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "doc_id",
            UUID(as_uuid=True),
            sa.ForeignKey("docs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("grantee_type", sa.Text, nullable=False),
        sa.Column("grantee_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.CheckConstraint(
            "(grantee_type = 'org' AND grantee_id IS NULL) OR "
            "(grantee_type IN ('person', 'team') AND grantee_id IS NOT NULL)",
            name="ck_doc_grants_grantee_shape",
        ),
        sa.UniqueConstraint("doc_id", "grantee_type", "grantee_id", name="uq_doc_grants_grantee"),
    )
    op.create_index("ix_doc_grants_doc", "doc_grants", ["doc_id"])
    op.create_index(
        "uq_doc_grants_org",
        "doc_grants",
        ["doc_id"],
        unique=True,
        postgresql_where=sa.text("grantee_type = 'org'"),
    )


def downgrade() -> None:
    op.drop_index("uq_doc_grants_org", table_name="doc_grants")
    op.drop_index("ix_doc_grants_doc", table_name="doc_grants")
    op.drop_table("doc_grants")
