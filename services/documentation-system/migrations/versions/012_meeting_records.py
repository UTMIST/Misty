"""Durable meeting hand-off, canonical Google identity and retry state.

Revision ID: 012
Revises: 011
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("docs", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "meeting_records",
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meeting_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("payload_hash", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("doc_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("docs.id"), nullable=True),
        sa.Column("google_document_id", sa.Text, nullable=True, unique=True),
        sa.Column("doc_url", sa.Text, nullable=True),
        sa.Column("guild_id", sa.Text, nullable=True),
        sa.Column("text_channel_id", sa.Text, nullable=True),
        sa.Column("voice_channel_id", sa.Text, nullable=True),
        sa.Column("requester_discord_id", sa.Text, nullable=True),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("discord_message_id", sa.Text, nullable=True),
        sa.Column("removed_by", sa.Text, nullable=True),
        sa.Column("requester_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("create_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("participants", postgresql.JSONB, nullable=False),
        sa.Column("decisions", postgresql.JSONB, nullable=False),
        sa.Column("action_items", postgresql.JSONB, nullable=False),
        sa.Column(
            "audience_team_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False
        ),
        sa.Column("audience_group_emails", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("pending_payload", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("persistence_status", sa.Text, nullable=False),
        sa.Column("create_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("delivery_status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_meeting_records_due", "meeting_records", ["next_attempt_at", "next_reconcile_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_meeting_records_due", table_name="meeting_records")
    op.drop_table("meeting_records")
    op.drop_column("docs", "purged_at")
