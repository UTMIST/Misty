"""Backfill Google source links while retaining exact URLs for rollback.

Revision ID: 017
Revises: 016
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "doc_source_url_backfill",
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("docs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("replacement_url", sa.Text(), nullable=False),
    )
    # Keep the snapshot and rewrite consistent with concurrent catalog writes.
    op.execute(sa.text("LOCK TABLE docs IN SHARE ROW EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            """
            INSERT INTO doc_source_url_backfill (doc_id, original_url, replacement_url)
            SELECT id, url,
                CASE source_id
                    WHEN 'gdocs' THEN 'https://docs.google.com/document/d/' || file_id || '/edit'
                    WHEN 'gsheets' THEN 'https://docs.google.com/spreadsheets/d/' || file_id || '/edit'
                    WHEN 'gslides' THEN 'https://docs.google.com/presentation/d/' || file_id || '/edit'
                    WHEN 'gdrive' THEN 'https://drive.google.com/file/d/' || file_id || '/view'
                END
            FROM (
                SELECT id, url, source_id,
                    substring(url_normalized FROM
                        '^https?://drive[.]google[.]com/file/d/([A-Za-z0-9_-]+)(?:[/?#]|$)')
                        AS file_id
                FROM docs
                WHERE source_id IN ('gdocs', 'gsheets', 'gslides', 'gdrive')
            ) AS candidates
            WHERE file_id IS NOT NULL
                AND substring(url FROM
                    '^https?://drive[.]google[.]com/file/d/([A-Za-z0-9_-]+)(?:[/?#]|$)') = file_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE docs AS d SET url = b.replacement_url
            FROM doc_source_url_backfill AS b WHERE d.id = b.doc_id
            """
        )
    )


def downgrade() -> None:
    # Do not overwrite links edited after upgrade or touch newly inserted docs.
    op.execute(
        sa.text(
            """
            UPDATE docs AS d SET url = b.original_url
            FROM doc_source_url_backfill AS b
            WHERE d.id = b.doc_id AND d.url = b.replacement_url
            """
        )
    )
    op.drop_table("doc_source_url_backfill")
