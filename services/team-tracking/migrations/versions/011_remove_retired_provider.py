"""Remove the retired identity provider and its current-state identifiers.

Revision ID: 011
Revises: 010
Create Date: 2026-10-18

Downgrade restores the provider vocabulary only. Deleted identity rows cannot
be recovered; restore a database backup if those links are needed again.
"""

from pathlib import Path
from runpy import run_path

from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None

# Read the immutable historical seed so retired vocabulary stays in migration 004.
_KEEP_PROVIDER_IDS = {"discord", "github", "uoft_email"}
RETIRED_PROVIDER_ID, _RETIRED_PROVIDER_LABEL = next(
    provider
    for provider in run_path(str(Path(__file__).with_name("004_person_identifiers.py")))[
        "PROVIDERS"
    ]
    if provider[0] not in _KEEP_PROVIDER_IDS
)


def upgrade() -> None:
    op.execute(
        sa.text("DELETE FROM person_identifiers WHERE provider = :provider").bindparams(
            provider=RETIRED_PROVIDER_ID
        )
    )
    op.execute(sa.text("DELETE FROM providers WHERE id = :id").bindparams(id=RETIRED_PROVIDER_ID))


def downgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO providers (id, label, created_by, updated_by) "
            "VALUES (:id, :label, 'system', 'system')"
        ).bindparams(id=RETIRED_PROVIDER_ID, label=_RETIRED_PROVIDER_LABEL)
    )
