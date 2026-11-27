"""Pure state-change validation shared by both email-submission storage adapters."""

from datetime import datetime, timezone

from contracts.email import EmailSubmission


def changed_email_submission(
    submission: EmailSubmission,
    values: dict,
    actor: str,
    *,
    now: datetime | None = None,
) -> EmailSubmission:
    immutable = (
        "submission_id",
        "mailbox_identity",
        "gmail_message_id",
        "gmail_thread_id",
        "submitter_person_id",
        "verified_sender_email",
        "received_at",
    )
    for field in immutable:
        if field in values and values[field] != getattr(submission, field):
            raise ValueError(f"{field} is immutable")
    if submission.receipt_sent_at is not None and (
        values.get("receipt_sent_at", submission.receipt_sent_at) != submission.receipt_sent_at
        or values.get("receipt_status", submission.receipt_status) != "sent"
    ):
        raise ValueError("confirmed receipt delivery is immutable")
    if submission.failure_notice_sent_at is not None and (
        values.get("failure_notice_sent_at", submission.failure_notice_sent_at)
        != submission.failure_notice_sent_at
        or values.get("failure_notice_status", submission.failure_notice_status) != "sent"
    ):
        raise ValueError("confirmed failure notice delivery is immutable")
    stamp = now or datetime.now(timezone.utc)
    return EmailSubmission.model_validate(
        submission.model_dump()
        | values
        | {
            "version": submission.version + 1,
            "updated_at": stamp,
            "updated_by": actor,
        }
    )
