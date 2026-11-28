from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from contracts.gmail import (
    GmailError,
    GmailInspectResult,
    GmailListPage,
    GmailListRequest,
    GmailMailbox,
    GmailReceiptCheckRequest,
    GmailReceiptCheckResult,
    GmailReplyRequest,
    GmailReplyResult,
)
from src.api.auth import require_scope
from src.api.deps import get_gmail_mailbox

router = APIRouter(prefix="/google/gmail", tags=["gmail"])
MessageId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,200}$")]

_VALIDATION_CODES = frozenset(
    {"invalid_reply_header", "invalid_reply_body", "invalid_recipient", "self_reply_forbidden"}
)


def _call(fn, *args):
    try:
        return fn(*args)
    except GmailError as exc:
        if exc.code in {"gmail_not_configured", "gmail_mailbox_mismatch"}:
            status = 503
        elif exc.code == "gmail_not_found":
            status = 404
        elif exc.code in _VALIDATION_CODES:
            status = 422
        elif exc.uncertain:
            status = 502
        else:
            status = 409
        raise HTTPException(status, detail={"code": exc.code, "uncertain": exc.uncertain}) from None


# Literal path registered before /messages/{message_id}.
@router.post("/messages/list", response_model=GmailListPage)
def list_messages(
    body: GmailListRequest,
    request: Request,
    _key=Depends(require_scope("email-read")),
    mailbox: GmailMailbox = Depends(get_gmail_mailbox),
) -> GmailListPage:
    page = _call(mailbox.list_messages, body)
    request.state.audit_extra = {"messages": len(page.messages)}
    return page


@router.post("/messages/{message_id}", response_model=GmailInspectResult)
def inspect_message(
    message_id: MessageId,
    request: Request,
    _key=Depends(require_scope("email-read")),
    mailbox: GmailMailbox = Depends(get_gmail_mailbox),
) -> GmailInspectResult:
    result = _call(mailbox.inspect_message, message_id)
    request.state.audit_extra = {"classification": result.classification}
    return result


@router.post("/reply", response_model=GmailReplyResult)
def send_reply(
    body: GmailReplyRequest,
    _key=Depends(require_scope("email-send")),
    mailbox: GmailMailbox = Depends(get_gmail_mailbox),
) -> GmailReplyResult:
    return _call(mailbox.send_reply, body)


@router.post("/receipt-check", response_model=GmailReceiptCheckResult)
def check_receipt(
    body: GmailReceiptCheckRequest,
    request: Request,
    _key=Depends(require_scope("email-read")),
    mailbox: GmailMailbox = Depends(get_gmail_mailbox),
) -> GmailReceiptCheckResult:
    result = _call(mailbox.check_receipt, body)
    request.state.audit_extra = {"sent": result.sent}
    return result
