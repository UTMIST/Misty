"""seed sources

Revision ID: 002
Revises: 001
Create Date: 2026-07-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

# (id, label, url_patterns, requires_auth, has_api, content_fetch_enabled)
SOURCES = [
    ("web", "Web page", [], False, False, True),
    ("github", "GitHub", ["github.com"], False, True, True),
    ("gdrive", "Google Drive", ["drive.google.com"], True, True, False),
    ("gdocs", "Google Docs", ["docs.google.com/document"], True, True, False),
    ("gsheets", "Google Sheets", ["docs.google.com/spreadsheets"], True, True, False),
    ("gslides", "Google Slides", ["docs.google.com/presentation"], True, True, False),
    ("notion", "Notion", ["notion.so", "notion.site"], True, True, False),
    ("youtube", "YouTube", ["youtube.com", "youtu.be"], False, True, False),
]


def upgrade() -> None:
    tbl = sa.table(
        "sources",
        sa.column("id", sa.Text),
        sa.column("label", sa.Text),
        sa.column("url_patterns", ARRAY(sa.Text)),
        sa.column("requires_auth", sa.Boolean),
        sa.column("has_api", sa.Boolean),
        sa.column("content_fetch_enabled", sa.Boolean),
        sa.column("created_by", sa.Text),
        sa.column("updated_by", sa.Text),
    )
    op.bulk_insert(
        tbl,
        [
            {
                "id": s[0],
                "label": s[1],
                "url_patterns": s[2],
                "requires_auth": s[3],
                "has_api": s[4],
                "content_fetch_enabled": s[5],
                "created_by": "system",
                "updated_by": "system",
            }
            for s in SOURCES
        ],
    )


def downgrade() -> None:
    ids = [s[0] for s in SOURCES]
    op.execute(sa.text("DELETE FROM sources WHERE id = ANY(:ids)").bindparams(ids=ids))
