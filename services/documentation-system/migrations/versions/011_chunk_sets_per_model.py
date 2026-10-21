"""Key document chunks by embedding model so two sets can coexist.

A re-embed after a model change (#209) builds the target model's set beside
the one search serves, then removes the old set after cutover. Existing rows
already carry one model per doc, so they satisfy the wider key unchanged.

Downgrade restores the (doc_id, ordinal) key and refuses while any doc still
holds more than one model's set: finish or abandon the re-embed first, rather
than have the migration guess which set to discard.

Revision ID: 011
Revises: 010
Create Date: 2026-10-21
"""

from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("doc_chunks_pkey", "doc_chunks", type_="primary")
    op.create_primary_key("doc_chunks_pkey", "doc_chunks", ["doc_id", "embedding_model", "ordinal"])


def downgrade() -> None:
    mixed = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM (SELECT doc_id FROM doc_chunks GROUP BY doc_id "
                "HAVING count(DISTINCT embedding_model) > 1) AS mixed"
            )
        )
        .scalar_one()
    )
    if mixed:
        raise RuntimeError(
            f"{mixed} docs hold chunk sets for more than one embedding model; finish the "
            "re-embed (or remove the unwanted sets) before downgrading below 011"
        )
    op.drop_constraint("doc_chunks_pkey", "doc_chunks", type_="primary")
    op.create_primary_key("doc_chunks_pkey", "doc_chunks", ["doc_id", "ordinal"])
