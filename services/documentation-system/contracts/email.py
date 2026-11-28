"""Durable forwarded-email receipt models and injected Gmail connector contract."""

from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


GmailId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")]

GmailClassification = Literal[
    "accepted_candidate",
    "ignored_non_inbox",
    "ignored_loop",
    "rejected_unauthenticated",
    "rejected_oversize",
]

AuthMechanism = Literal["dmarc_pass", "dkim_aligned_no_dmarc"]

SubmissionStatus = Literal["accepted", "rejected_oversize", "failed"]
EmailPersistenceStatus = Literal["pending", "not_applicable", "failed"]

ReceiptStatus = Literal[
    "pending",
    "sending",
    "sent",
    "uncertain",
    "suppressed_rate_limit",
    "failed",
]

FailureNoticeStatus = Literal[
    "none",
    "pending",
    "sending",
    "sent",
    "uncertain",
    "suppressed_rate_limit",
    "failed",
]

ProcessedOutcome = Literal[
    "accepted",
    "rejected_oversize",
    "duplicate_submission",
    "ignored_non_inbox",
    "ignored_loop",
    "rejected_unauthenticated",
    "ignored_unknown_sender",
]


class GmailMessageRef(StrictModel):
    message_id: GmailId
    thread_id: GmailId


class GmailListPage(StrictModel):
    mailbox_address: str = Field(min_length=3, max_length=320)
    messages: list[GmailMessageRef] = Field(default_factory=list, max_length=100)
    next_page_token: str | None = Field(default=None, min_length=1, max_length=4096)


class ClaimedHeaders(StrictModel):
    from_raw: str | None = Field(default=None, max_length=1000, repr=False)
    date_raw: str | None = Field(default=None, max_length=500, repr=False)
    subject_raw: str | None = Field(default=None, max_length=1000, repr=False)
    to_raw: str | None = Field(default=None, max_length=2000, repr=False)
    cc_raw: str | None = Field(default=None, max_length=2000, repr=False)


class OmittedAttachment(StrictModel):
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)


class GmailInspectResult(StrictModel):
    classification: GmailClassification
    mailbox_address: str = Field(min_length=3, max_length=320)
    message_id: GmailId
    thread_id: GmailId
    received_at: AwareDatetime
    verified_sender_email: str | None = Field(default=None, max_length=320, repr=False)
    auth_mechanism: AuthMechanism | None = None
    outer_message_id: str | None = Field(default=None, max_length=998, repr=False)
    subject: str | None = Field(default=None, max_length=1000, repr=False)
    forwarder_note: str = Field(default="", max_length=1_200_000, repr=False)
    forwarded_body: str = Field(default="", max_length=1_200_000, repr=False)
    delimiter_detected: bool = False
    truncated: bool = False
    omitted_attachment_count: int = Field(default=0, ge=0)
    omitted_attachments: list[OmittedAttachment] = Field(default_factory=list, max_length=200)
    untrusted_claimed_headers: ClaimedHeaders = Field(default_factory=ClaimedHeaders, repr=False)
    warnings: list[str] = Field(default_factory=list)


class EmailSubmission(StrictModel):
    submission_id: UUID
    version: int = 0
    mailbox_identity: str = Field(min_length=3, max_length=320)
    gmail_message_id: GmailId
    gmail_thread_id: GmailId
    outer_message_id: str | None = Field(default=None, max_length=998, repr=False)
    submitter_person_id: UUID
    submitter_label: str | None = None
    verified_sender_email: str = Field(min_length=3, max_length=320, repr=False)
    auth_mechanism: AuthMechanism
    received_at: AwareDatetime
    subject: str | None = Field(default=None, max_length=1000, repr=False)
    status: SubmissionStatus = "accepted"
    persistence_status: EmailPersistenceStatus = "pending"
    delimiter_detected: bool = False
    truncated: bool = False
    omitted_attachment_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    pending_payload: dict | None = Field(default=None, repr=False)
    failed_at: AwareDatetime | None = None
    receipt_status: ReceiptStatus = "pending"
    receipt_attempt_id: UUID | None = None
    receipt_lease_until: AwareDatetime | None = None
    receipt_attempts: int = 0
    receipt_sent_at: AwareDatetime | None = None
    receipt_gmail_message_id: str | None = None
    failure_notice_status: FailureNoticeStatus = "none"
    failure_notice_attempt_id: UUID | None = None
    failure_notice_lease_until: AwareDatetime | None = None
    failure_notice_attempts: int = 0
    failure_notice_sent_at: AwareDatetime | None = None
    failure_notice_gmail_message_id: str | None = None
    next_attempt_at: AwareDatetime | None = None
    last_error: str | None = None
    notification_last_error: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    created_by: str
    updated_by: str

    def public(self) -> dict:
        return self.model_dump(
            exclude={
                "pending_payload",
                "verified_sender_email",
                "outer_message_id",
            }
        )


class EmailPollResult(StrictModel):
    listed: int = Field(default=0, strict=True, ge=0)
    accepted: int = Field(default=0, strict=True, ge=0)
    duplicates: int = Field(default=0, strict=True, ge=0)
    rejected_oversize: int = Field(default=0, strict=True, ge=0)
    ignored_non_inbox: int = Field(default=0, strict=True, ge=0)
    ignored_loop: int = Field(default=0, strict=True, ge=0)
    rejected_unauthenticated: int = Field(default=0, strict=True, ge=0)
    ignored_unknown_sender: int = Field(default=0, strict=True, ge=0)
    directory_deferred: int = Field(default=0, strict=True, ge=0)
    inspect_failed: int = Field(default=0, strict=True, ge=0)
    receipts_attempted: int = Field(default=0, strict=True, ge=0)
    receipts_sent: int = Field(default=0, strict=True, ge=0)
    receipts_suppressed: int = Field(default=0, strict=True, ge=0)
    receipts_failed: int = Field(default=0, strict=True, ge=0)
    expired_cleaned: int = Field(default=0, strict=True, ge=0)
    complete: bool = True
    next_page_token: str | None = Field(default=None, min_length=1, max_length=4096)


class GmailError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class GmailClient(Protocol):
    def list_messages(
        self, *, after_epoch_s: int | None, page_token: str | None, page_size: int
    ) -> GmailListPage: ...

    def inspect_message(self, message_id: str) -> GmailInspectResult: ...

    def send_reply(
        self,
        *,
        thread_id: str,
        to_email: str,
        subject: str,
        body_text: str,
        in_reply_to: str | None,
        receipt_id: UUID,
    ) -> str: ...

    def check_receipt(
        self,
        *,
        thread_id: str,
        receipt_id: UUID,
        sent_after_epoch_s: int | None = None,
    ) -> str | None: ...
