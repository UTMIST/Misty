"""docs: at-most-one active row per normalized URL

Enforces the dedup invariant at the DB level (bug #11): a partial unique index
on url_normalized WHERE active. Inactive (soft-removed) rows are exempt, so a
URL can be re-catalogued after removal.

The index cannot be created directly on live data because duplicate active rows
already exist (bug #5 let them proliferate). So we FIRST canonicalize: for each
url_normalized with more than one active row, keep exactly the row that
get_doc_by_normalized_url now prefers — the earliest active created_at (id as a
final deterministic tiebreak) — and soft-remove (active=False) the rest. Only
then is the partial unique index created.

Revision ID: 004
Revises: 003
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Backfill: collapse each set of duplicate active rows down to a single
    #    canonical active row, matching the get_doc_by_normalized_url rule
    #    (earliest created_at, id tiebreak). Must run BEFORE the unique index or
    #    the index creation fails on existing duplicates.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY url_normalized
                           ORDER BY created_at, id
                       ) AS rn
                FROM docs
                WHERE active
            )
            UPDATE docs
            SET active = false,
                updated_at = now()
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """
        )
    )

    # 2. Now the invariant holds; create the partial unique index.
    op.create_index(
        "uq_docs_url_normalized_active",
        "docs",
        ["url_normalized"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    # Only the index is reversible; the backfill's soft-removals are left in
    # place (there is no safe way to know which rows to reactivate).
    op.drop_index("uq_docs_url_normalized_active", table_name="docs")
