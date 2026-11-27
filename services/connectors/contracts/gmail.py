"""Authenticated Gmail inbound and receipt boundary. No vendor or app imports."""

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


class GmailListRequest(StrictModel):
    after_epoch_s: int | None = Field(default=None, ge=0)
    page_token: str | None = Field(default=None, min_length=1, max_length=4096)
    page_size: int = Field(default=50, ge=1, le=100)


class GmailMessageRef(StrictModel):
    message_id: GmailId
    thread_id: GmailId


class GmailListPage(StrictModel):
    mailbox_address: str = Field(min_length=3, max_length=320)
    messages: list[GmailMessageRef] = Field(default_factory=list, max_length=100)
    next_page_token: str | None = Field(default=None, min_length=1, max_length=4096)


class ClaimedHeaders(StrictModel):
    """Untrusted headers parsed from quoted text; never used as identity or access."""

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


class GmailReplyRequest(StrictModel):
    thread_id: GmailId
    to_email: str = Field(min_length=3, max_length=320, repr=False)
    subject: str = Field(min_length=1, max_length=500, repr=False)
    body_text: str = Field(min_length=1, max_length=50_000, repr=False)
    in_reply_to: str | None = Field(default=None, max_length=998, repr=False)
    receipt_id: UUID


class GmailReplyResult(StrictModel):
    sent_message_id: GmailId
    thread_id: GmailId


class GmailReceiptCheckRequest(StrictModel):
    thread_id: GmailId
    receipt_id: UUID


class GmailReceiptCheckResult(StrictModel):
    sent: bool
    sent_message_id: GmailId | None = None


class GmailError(Exception):
    """Content-free code; uncertain=True forbids blind send retries."""

    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class GmailMailbox(Protocol):
    def list_messages(self, body: GmailListRequest) -> GmailListPage: ...
    def inspect_message(self, message_id: str) -> GmailInspectResult: ...
    def send_reply(self, body: GmailReplyRequest) -> GmailReplyResult: ...
    def check_receipt(self, body: GmailReceiptCheckRequest) -> GmailReceiptCheckResult: ...
