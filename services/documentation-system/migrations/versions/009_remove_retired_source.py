"""Remove the retired catalog source, preserving its documents as web links.

Revision ID: 009
Revises: 008
Create Date: 2026-10-18

Downgrade restores the vocabulary only. Document reassignment is permanent:
we cannot distinguish migrated documents from web links added after upgrade.
"""

from pathlib import Path
from runpy import run_path

from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

# Read the immutable historical seed so retired vocabulary stays in migration 002.
_KEEP_SOURCE_IDS = {"web", "github", "gdrive", "gdocs", "gsheets", "gslides", "youtube"}
_RETIRED_SOURCE = next(
    source
    for source in run_path(str(Path(__file__).with_name("002_seed_sources.py")))["SOURCES"]
    if source[0] not in _KEEP_SOURCE_IDS
)
RETIRED_SOURCE_ID = _RETIRED_SOURCE[0]


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE docs SET source_id = 'web', updated_at = now(), updated_by = 'migration_009' "
            "WHERE source_id = :source_id"
        ).bindparams(source_id=RETIRED_SOURCE_ID)
    )
    op.execute(sa.text("DELETE FROM sources WHERE id = :id").bindparams(id=RETIRED_SOURCE_ID))


def downgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO sources "
            "(id, label, url_patterns, requires_auth, has_api, content_fetch_enabled, "
            "created_by, updated_by) "
            "VALUES (:id, :label, :patterns, true, true, false, "
            "'system', 'system')"
        ).bindparams(
            sa.bindparam("id", RETIRED_SOURCE_ID),
            sa.bindparam("label", _RETIRED_SOURCE[1]),
            sa.bindparam("patterns", _RETIRED_SOURCE[2], type_=sa.ARRAY(sa.Text)),
        )
    )
