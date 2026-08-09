"""sources: enable content fetching for the Google sources

The Google sources were seeded content_fetch_enabled=false in migration 002
because no fetcher existed. The connectors service now provides one, so ingest
should attempt a fetch. A missing credential is non-fatal: the fetch fails,
ingest records a warning, and the doc is still catalogued.

Revision ID: 006
Revises: 005
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None

_GOOGLE_SOURCE_IDS = ("gdocs", "gsheets", "gslides", "gdrive")


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE sources SET content_fetch_enabled = true, updated_at = now() WHERE id IN :ids"
        ).bindparams(sa.bindparam("ids", value=_GOOGLE_SOURCE_IDS, expanding=True))
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE sources SET content_fetch_enabled = false, updated_at = now() WHERE id IN :ids"
        ).bindparams(sa.bindparam("ids", value=_GOOGLE_SOURCE_IDS, expanding=True))
    )
