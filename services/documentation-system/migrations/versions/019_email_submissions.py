"""Durable forwarded-email submissions, inbox watermark, poller guard and send quotas.

Revision ID: 019
Revises: 018
Create Date: 2026-11-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_poll_run",
        sa.Column("singleton", sa.Boolean(), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint("singleton", name="ck_email_poll_run_singleton"),
    )
    op.create_table(
        "email_inbox_state",
        sa.Column("mailbox_identity", sa.Text(), primary_key=True),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by", sa.Text(), nullable=False),
    )
    op.create_table(
        "email_submissions",
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("mailbox_identity", sa.Text(), nullable=False),
        sa.Column("gmail_message_id", sa.Text(), nullable=False),
        sa.Column("gmail_thread_id", sa.Text(), nullable=False),
        sa.Column("outer_message_id", sa.Text(), nullable=True),
        sa.Column("submitter_person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submitter_label", sa.Text(), nullable=True),
        sa.Column("verified_sender_email", sa.Text(), nullable=False),
        sa.Column("auth_mechanism", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("persistence_status", sa.Text(), nullable=False),
        sa.Column(
            "delimiter_detected",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "omitted_attachment_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "warnings",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("ARRAY[]::text[]"),
        ),
        sa.Column("pending_payload", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_status", sa.Text(), nullable=False),
        sa.Column("receipt_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("receipt_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("receipt_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_gmail_message_id", sa.Text(), nullable=True),
        sa.Column(
            "failure_notice_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
        sa.Column("failure_notice_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("failure_notice_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "failure_notice_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("failure_notice_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_notice_gmail_message_id", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("notification_last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "mailbox_identity",
            "gmail_message_id",
            name="uq_email_submissions_mailbox_gmail_msg",
        ),
    )
    op.create_index(
        "uq_email_submissions_submitter_outer_msgid",
        "email_submissions",
        ["mailbox_identity", "submitter_person_id", "outer_message_id"],
        unique=True,
        postgresql_where=sa.text("outer_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_email_submissions_submitter",
        "email_submissions",
        ["submitter_person_id", "created_at"],
    )
    op.create_index(
        "ix_email_submissions_due",
        "email_submissions",
        ["next_attempt_at"],
    )
    op.create_table(
        "email_processed_messages",
        sa.Column("mailbox_identity", sa.Text(), primary_key=True),
        sa.Column("gmail_message_id", sa.Text(), primary_key=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("email_submissions.submission_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_email_processed_messages_at",
        "email_processed_messages",
        ["processed_at"],
    )
    op.create_table(
        "email_outbound_sends",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_email_outbound_sends_sent_at",
        "email_outbound_sends",
        ["sent_at"],
    )
    op.create_index(
        "ix_email_outbound_sends_person_sent_at",
        "email_outbound_sends",
        ["person_id", "sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_outbound_sends_person_sent_at", table_name="email_outbound_sends")
    op.drop_index("ix_email_outbound_sends_sent_at", table_name="email_outbound_sends")
    op.drop_table("email_outbound_sends")
    op.drop_index("ix_email_processed_messages_at", table_name="email_processed_messages")
    op.drop_table("email_processed_messages")
    op.drop_index("ix_email_submissions_due", table_name="email_submissions")
    op.drop_index("ix_email_submissions_submitter", table_name="email_submissions")
    op.drop_index("uq_email_submissions_submitter_outer_msgid", table_name="email_submissions")
    op.drop_table("email_submissions")
    op.drop_table("email_inbox_state")
    op.drop_table("email_poll_run")
