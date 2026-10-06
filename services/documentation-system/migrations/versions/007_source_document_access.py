"""source-derived document access with provenance and expiry

Revision ID: 007
Revises: 006
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "docs", sa.Column("source_access_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "docs", sa.Column("source_access_attempted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "doc_grants",
        sa.Column("origin", sa.Text(), nullable=False, server_default=sa.text("'manual'")),
    )
    op.add_column("doc_grants", sa.Column("source_permission_id", sa.Text(), nullable=True))
    op.add_column("doc_grants", sa.Column("source_principal", sa.Text(), nullable=True))
    op.add_column("doc_grants", sa.Column("source_role", sa.Text(), nullable=True))
    op.add_column(
        "doc_grants",
        sa.Column(
            "source_inherited", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "doc_grants", sa.Column("source_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "doc_grants",
        sa.Column(
            "source_inherited_from",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("ARRAY[]::text[]"),
        ),
    )
    op.add_column("doc_grants", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))

    op.drop_constraint("uq_doc_grants_grantee", "doc_grants", type_="unique")
    op.drop_index("uq_doc_grants_org", table_name="doc_grants")
    op.create_index(
        "uq_doc_grants_manual_grantee",
        "doc_grants",
        ["doc_id", "grantee_type", "grantee_id"],
        unique=True,
        postgresql_where=sa.text("origin = 'manual' AND grantee_id IS NOT NULL"),
    )
    op.create_index(
        "uq_doc_grants_org",
        "doc_grants",
        ["doc_id"],
        unique=True,
        postgresql_where=sa.text("origin = 'manual' AND grantee_type = 'org'"),
    )
    op.create_index(
        "uq_doc_grants_source_permission",
        "doc_grants",
        ["doc_id", "origin", "source_permission_id", "grantee_type", "grantee_id"],
        unique=True,
        postgresql_where=sa.text("origin <> 'manual'"),
    )


def downgrade() -> None:
    # Source-derived rows have no representation in the pre-007 manual-only
    # schema. Remove only those rows; all manual grants survive the downgrade.
    op.execute(sa.text("DELETE FROM doc_grants WHERE origin <> 'manual'"))
    op.drop_index("uq_doc_grants_source_permission", table_name="doc_grants")
    op.drop_index("uq_doc_grants_org", table_name="doc_grants")
    op.drop_index("uq_doc_grants_manual_grantee", table_name="doc_grants")
    op.create_unique_constraint(
        "uq_doc_grants_grantee", "doc_grants", ["doc_id", "grantee_type", "grantee_id"]
    )
    op.create_index(
        "uq_doc_grants_org",
        "doc_grants",
        ["doc_id"],
        unique=True,
        postgresql_where=sa.text("grantee_type = 'org'"),
    )

    op.drop_column("doc_grants", "expires_at")
    op.drop_column("doc_grants", "source_inherited_from")
    op.drop_column("doc_grants", "source_inherited")
    op.drop_column("doc_grants", "source_role")
    op.drop_column("doc_grants", "source_expires_at")
    op.drop_column("doc_grants", "source_principal")
    op.drop_column("doc_grants", "source_permission_id")
    op.drop_column("doc_grants", "origin")
    op.drop_column("docs", "source_access_attempted_at")
    op.drop_column("docs", "source_access_synced_at")
