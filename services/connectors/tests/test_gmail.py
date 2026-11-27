import base64
from email.message import EmailMessage
import email
import email.policy
import json
import logging
from uuid import uuid4

import httplib2
import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from platform_auth import key_store_from_config

from contracts.gmail import (
    GmailError,
    GmailListRequest,
    GmailReceiptCheckRequest,
    GmailReplyRequest,
)
from src.api.app import create_app
from src.api.deps import get_gmail_mailbox, get_key_store
from src.api.hashing import generate_key
from src.sources.gmail_google import GoogleGmailMailbox, _execute

MISTY_ADDR = "misty@utmist.ca"
AUTH = {"X-API-Key": "dev-api-key-change-me"}
INTERNAL_MS = "1764200000000"


class FakeRequest:
    def __init__(self, value):
        self.value = value

    def execute(self, **kwargs):
        assert kwargs == {"num_retries": 0}
        return self.value() if callable(self.value) else self.value


class FakeMessages:
    def __init__(self, gmail):
        self.g = gmail

    def list(self, **kwargs):
        self.g.list_calls.append(kwargs)
        resp = self.g.list_response
        if callable(resp):
            return FakeRequest(lambda: resp(kwargs))
        return FakeRequest(resp)

    def get(self, *, userId, id, format, **kwargs):
        assert userId == "me"
        self.g.get_calls.append((id, format))
        if format == "metadata":
            return FakeRequest(self.g.metadata[id])
        if format == "raw":
            return FakeRequest(self.g.raw[id])
        raise AssertionError(f"unexpected format {format}")

    def send(self, *, userId, body):
        assert userId == "me"
        self.g.sent_calls.append(body)
        return FakeRequest(self.g.send_response)


class FakeThreads:
    def __init__(self, gmail):
        self.g = gmail

    def get(self, *, userId, id, format, **kwargs):
        assert userId == "me"
        assert format == "metadata"
        self.g.thread_calls.append((id, kwargs))
        return FakeRequest(self.g.threads.get(id, {"id": id, "messages": []}))


class FakeUsers:
    def __init__(self, gmail):
        self.g = gmail

    def messages(self):
        return FakeMessages(self.g)

    def threads(self):
        return FakeThreads(self.g)


class FakeGmail:
    def __init__(self):
        self.list_calls = []
        self.get_calls = []
        self.sent_calls = []
        self.thread_calls = []
        self.list_response = {"messages": []}
        self.metadata = {}
        self.raw = {}
        self.threads = {}
        self.send_response = {"id": "sent_1", "threadId": "thr_1"}

    def users(self):
        return FakeUsers(self)


def make_raw_email(
    *,
    from_headers=("Alex Chen <alex.chen@mail.utoronto.ca>",),
    auth_headers=(
        "mx.google.com; dkim=pass header.i=@utoronto.ca header.s=selector1; "
        "spf=pass (google.com: domain of alex.chen@mail.utoronto.ca) "
        "smtp.mailfrom=alex.chen@mail.utoronto.ca; "
        "dmarc=pass (p=QUARANTINE sp=QUARANTINE dis=NONE) header.from=utoronto.ca",
    ),
    subject="Fwd: Sponsor Commitment 2026",
    message_id="<outer-123@mail.utoronto.ca>",
    extra_headers=(),
    plain_body=(
        "Sharing sponsor confirmation for archival.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Jordan Sponsor <jordan@acme.example>\n"
        "Date: Thu, Nov 26, 2026 at 3:15 PM\n"
        "Subject: Sponsor Commitment 2026\n"
        "To: Alex Chen <alex.chen@mail.utoronto.ca>\n"
        "Cc: partnerships@acme.example\n\n"
        "We confirm our Gold tier sponsorship of CAD 5,000."
    ),
    html_body=None,
    attachment_bytes=None,
    labels=("INBOX", "UNREAD"),
    size_estimate=None,
):
    msg = EmailMessage()
    for h in auth_headers:
        msg["Authentication-Results"] = h
    if from_headers:
        msg["From"] = from_headers[0]
    msg["To"] = MISTY_ADDR
    if subject is not None:
        msg["Subject"] = subject
    if message_id is not None:
        msg["Message-ID"] = message_id
    for k, v in extra_headers:
        msg[k] = v

    if html_body is not None and plain_body is None:
        msg.set_content(html_body, subtype="html")
    else:
        msg.set_content(plain_body or "")
        if html_body is not None:
            msg.add_alternative(html_body, subtype="html")

    if attachment_bytes is not None:
        msg.add_attachment(
            attachment_bytes,
            maintype="application",
            subtype="pdf",
            filename="sensitive-contract.pdf",
        )

    raw_bytes = msg.as_bytes()
    meta_headers = [{"name": k, "value": v} for k, v in msg.items()]
    for extra_from in from_headers[1:]:
        raw_bytes = f"From: {extra_from}\r\n".encode("utf-8") + raw_bytes
        meta_headers.append({"name": "From", "value": extra_from})
    meta = {
        "id": "msg_1",
        "threadId": "thr_1",
        "labelIds": list(labels),
        "internalDate": INTERNAL_MS,
        "sizeEstimate": len(raw_bytes) if size_estimate is None else size_estimate,
        "payload": {"headers": meta_headers},
    }
    raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    return meta, {"id": "msg_1", "threadId": "thr_1", "raw": raw_b64}


def build_mailbox(gmail, **kwargs):
    return GoogleGmailMailbox(address=MISTY_ADDR, gmail=gmail, **kwargs)


def test_list_and_inspect_authenticated_forward_separates_note_and_claimed_headers():
    gmail = FakeGmail()
    gmail.list_response = {
        "messages": [{"id": "msg_1", "threadId": "thr_1"}],
        "nextPageToken": "tok_2",
    }
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email()
    mb = build_mailbox(gmail)

    page = mb.list_messages(GmailListRequest(after_epoch_s=1700000000, page_size=25))
    assert page.mailbox_address == MISTY_ADDR
    assert [m.message_id for m in page.messages] == ["msg_1"]
    assert page.next_page_token == "tok_2"
    call = gmail.list_calls[0]
    assert call["labelIds"] == ["INBOX"]
    assert call["includeSpamTrash"] is False
    assert "after:1700000000" in call["q"]
    assert "-in:sent" in call["q"]

    res = mb.inspect_message("msg_1")
    assert res.classification == "accepted_candidate"
    assert res.verified_sender_email == "alex.chen@mail.utoronto.ca"
    assert res.auth_mechanism == "dmarc_pass"
    assert res.outer_message_id == "<outer-123@mail.utoronto.ca>"
    assert res.subject == "Fwd: Sponsor Commitment 2026"
    assert res.delimiter_detected is True
    assert res.forwarder_note == "Sharing sponsor confirmation for archival."
    assert "Gold tier sponsorship" in res.forwarded_body
    # Claimed original author stays in untrusted_claimed_headers, never verified_sender_email.
    assert res.untrusted_claimed_headers.from_raw == "Jordan Sponsor <jordan@acme.example>"
    assert res.untrusted_claimed_headers.subject_raw == "Sponsor Commitment 2026"
    assert res.untrusted_claimed_headers.cc_raw == "partnerships@acme.example"
    # repr() hides sensitive email/body fields.
    assert "alex.chen@mail.utoronto.ca" not in repr(res)
    assert "Gold tier sponsorship" not in repr(res)


def test_spoofed_from_or_forged_injected_auth_results_rejected_before_raw_download():
    gmail = FakeGmail()
    # Attacker injects a fake Authentication-Results header claiming mx.google.com dmarc=pass,
    # while Google's real prepended topmost header reports spf=fail and dmarc=fail.
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email(
        from_headers=("president@utmist.ca",),
        auth_headers=(
            "mx.google.com; spf=fail (google.com: domain does not designate 203.0.113.9) "
            "smtp.mailfrom=president@utmist.ca; dmarc=fail (p=REJECT) header.from=utmist.ca",
            "mx.google.com; dkim=pass header.d=utmist.ca; dmarc=pass header.from=utmist.ca",
        ),
    )
    res = build_mailbox(gmail).inspect_message("msg_1")
    assert res.classification == "rejected_unauthenticated"
    assert res.verified_sender_email is None
    assert res.forwarded_body == ""
    assert gmail.get_calls == [("msg_1", "metadata")]


@pytest.mark.parametrize(
    "from_headers,auth_headers",
    [
        # Wrong authserv-id at top (not mx.google.com)
        (
            ("member@utmist.ca",),
            ("mail.attacker.example; dmarc=pass header.from=utmist.ca",),
        ),
        # No Authentication-Results header at all
        (("member@utmist.ca",), ()),
        # Dual From headers (RFC 5322 DKIM header smuggling)
        (
            ("victim@utmist.ca", "attacker@evil.example"),
            ("mx.google.com; dmarc=pass header.from=evil.example",),
        ),
        # Multiple mailboxes in a single From header
        (
            ("victim@utmist.ca, attacker@evil.example",),
            ("mx.google.com; dmarc=pass header.from=evil.example",),
        ),
        # Display-name spoofing where DMARC passed for utmist.ca? No: DMARC header.from=evil.example
        # mismatches victim@utmist.ca if forged, and resolves strictly to attacker@evil.example!
        (
            ('"victim@utmist.ca" <attacker@evil.example>',),
            ("mx.google.com; dmarc=pass header.from=utmist.ca",),
        ),
        # dmarc=none with public-suffix DKIM (co.uk must never align with victim.co.uk)
        (
            ("member@victim.co.uk",),
            ("mx.google.com; dmarc=none; dkim=pass header.d=co.uk; spf=pass",),
        ),
        # dmarc=none with unaligned gappssmtp.com DKIM
        (
            ("member@custom.example",),
            (
                "mx.google.com; dmarc=none; "
                "dkim=pass header.d=custom-example.20230601.gappssmtp.com; spf=pass",
            ),
        ),
        # dmarc=none with SPF pass only (no aligned DKIM signature)
        (
            ("member@custom.example",),
            ("mx.google.com; dmarc=none; spf=pass smtp.mailfrom=member@custom.example",),
        ),
    ],
)
def test_header_smuggling_and_alignment_attacks_rejected_before_raw_download(
    from_headers, auth_headers
):
    gmail = FakeGmail()
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email(
        from_headers=from_headers, auth_headers=auth_headers
    )
    res = build_mailbox(gmail).inspect_message("msg_1")
    assert res.classification == "rejected_unauthenticated"
    assert res.verified_sender_email is None
    assert gmail.get_calls == [("msg_1", "metadata")]


def test_display_name_spoof_resolves_actual_addr_spec_and_ignores_resent_from():
    gmail = FakeGmail()
    # Attacker owns evil.example (so evil.example passes DMARC), sets display name AND
    # Resent-From / X-Forwarded-To to impersonate a Misty member.
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email(
        from_headers=('"president@utmist.ca" <attacker@evil.example>',),
        auth_headers=("mx.google.com; dmarc=pass header.from=evil.example",),
        extra_headers=(
            ("Resent-From", "president@utmist.ca"),
            ("X-Forwarded-To", "president@utmist.ca"),
        ),
    )
    res = build_mailbox(gmail).inspect_message("msg_1")
    assert res.classification == "accepted_candidate"
    # Must resolve strictly to attacker@evil.example so directory lookup rejects it!
    assert res.verified_sender_email == "attacker@evil.example"


def test_raw_mime_from_discrepancy_is_rejected():
    gmail = FakeGmail()
    meta, raw = make_raw_email(
        from_headers=("alex@utmist.ca",),
        auth_headers=("mx.google.com; dmarc=pass header.from=utmist.ca",),
    )
    # Metadata shows one From, but raw bytes smuggle a second From header.
    decoded = base64.urlsafe_b64decode(raw["raw"] + "===")
    tampered = b"From: attacker@evil.example\r\n" + decoded
    raw["raw"] = base64.urlsafe_b64encode(tampered).decode("ascii").rstrip("=")
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = meta, raw
    res = build_mailbox(gmail).inspect_message("msg_1")
    assert res.classification == "rejected_unauthenticated"
    assert res.verified_sender_email is None


def test_dmarc_none_with_aligned_organizational_dkim_passes():
    gmail = FakeGmail()
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email(
        from_headers=("member@dept.custom-uni.edu",),
        auth_headers=(
            "mx.google.com; dkim=pass header.i=@custom-uni.edu header.s=sel1; "
            "spf=neutral; dmarc=none",
        ),
    )
    res = build_mailbox(gmail).inspect_message("msg_1")
    assert res.classification == "accepted_candidate"
    assert res.auth_mechanism == "dkim_aligned_no_dmarc"
    assert res.verified_sender_email == "member@dept.custom-uni.edu"


@pytest.mark.parametrize(
    "labels,extra_headers,from_hdr,expected",
    [
        (("INBOX", "SPAM"), (), "alex@utmist.ca", "ignored_non_inbox"),
        (("SENT",), (), "alex@utmist.ca", "ignored_non_inbox"),
        (("TRASH",), (), "alex@utmist.ca", "ignored_non_inbox"),
        (("INBOX",), (), MISTY_ADDR, "ignored_loop"),
        (("INBOX",), (("Return-Path", "<>"),), "alex@utmist.ca", "ignored_loop"),
        (("INBOX",), (("Return-Path", f"<{MISTY_ADDR}>"),), "alex@utmist.ca", "ignored_loop"),
        (("INBOX",), (("Auto-Submitted", "auto-replied"),), "alex@utmist.ca", "ignored_loop"),
        (("INBOX",), (("Precedence", "bulk"),), "alex@utmist.ca", "ignored_loop"),
        (("INBOX",), (("X-Autoreply", "yes"),), "alex@utmist.ca", "ignored_loop"),
        (("INBOX",), (("List-Id", "<announce.utmist.ca>"),), "alex@utmist.ca", "ignored_loop"),
    ],
)
def test_non_inbox_and_auto_reply_loops_ignored_before_raw_download(
    labels, extra_headers, from_hdr, expected
):
    gmail = FakeGmail()
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email(
        from_headers=(from_hdr,),
        auth_headers=("mx.google.com; dmarc=pass header.from=utmist.ca",),
        extra_headers=extra_headers,
        labels=labels,
    )
    res = build_mailbox(gmail).inspect_message("msg_1")
    assert res.classification == expected
    assert gmail.get_calls == [("msg_1", "metadata")]


def test_auto_submitted_no_is_permitted():
    gmail = FakeGmail()
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email(
        from_headers=("alex@utmist.ca",),
        auth_headers=("mx.google.com; dmarc=pass header.from=utmist.ca",),
        extra_headers=(("Auto-Submitted", "no"),),
    )
    res = build_mailbox(gmail).inspect_message("msg_1")
    assert res.classification == "accepted_candidate"


def test_oversize_metadata_and_oversize_raw_rejected_while_preserving_sender_for_notice():
    gmail = FakeGmail()
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email(
        size_estimate=6 * 1024 * 1024,
    )
    res = build_mailbox(gmail, max_email_bytes=5 * 1024 * 1024).inspect_message("msg_1")
    assert res.classification == "rejected_oversize"
    assert res.verified_sender_email == "alex.chen@mail.utoronto.ca"
    assert res.outer_message_id == "<outer-123@mail.utoronto.ca>"
    # Precheck caught it before fetching raw MIME!
    assert gmail.get_calls == [("msg_1", "metadata")]

    # Stale metadata sizeEstimate smaller than actual raw bytes still trips raw byte cap.
    gmail2 = FakeGmail()
    gmail2.metadata["msg_1"], gmail2.raw["msg_1"] = make_raw_email(
        plain_body="x" * 2000,
        size_estimate=100,
    )
    res2 = build_mailbox(gmail2, max_email_bytes=500).inspect_message("msg_1")
    assert res2.classification == "rejected_oversize"
    assert res2.verified_sender_email == "alex.chen@mail.utoronto.ca"


def test_html_conversion_omitted_attachments_and_text_truncation():
    gmail = FakeGmail()
    html = (
        "<html><head><style>body{}</style><script>alert(1)</script></head>"
        "<body><p>FYI team</p><p>Begin forwarded message:</p>"
        "<p>From: Event Venue &lt;venue@example.com&gt;<br>"
        "Date: Nov 20, 2026<br>Subject: Room Booking</p>"
        "<p>Room BA 1160 confirmed.</p></body></html>"
    )
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email(
        plain_body=None,
        html_body=html,
        attachment_bytes=b"%PDF-1.4 fake bytes",
    )
    res = build_mailbox(gmail, max_email_text_chars=90).inspect_message("msg_1")
    assert res.classification == "accepted_candidate"
    assert res.omitted_attachment_count == 1
    assert res.omitted_attachments[0].content_type == "application/pdf"
    assert res.truncated is True
    assert set(res.warnings) == {"attachments omitted", "converted from html", "content truncated"}
    assert "alert(1)" not in res.forwarder_note
    assert "alert(1)" not in res.forwarded_body


def test_send_reply_sets_loop_headers_rejects_injection_and_checks_receipt():
    gmail = FakeGmail()
    mb = build_mailbox(gmail)
    rid = uuid4()

    sent = mb.send_reply(
        GmailReplyRequest(
            thread_id="thr_1",
            to_email="Alex.Chen@mail.utoronto.ca",
            subject="Re: Fwd: Sponsor Commitment 2026",
            body_text="Received by Misty.",
            in_reply_to="<outer-123@mail.utoronto.ca>",
            receipt_id=rid,
        )
    )
    assert sent.sent_message_id == "sent_1"
    raw_sent = base64.urlsafe_b64decode(gmail.sent_calls[0]["raw"] + "===")
    parsed = email.message_from_bytes(raw_sent, policy=email.policy.default)
    assert parsed["From"] == MISTY_ADDR
    assert parsed["To"] == "alex.chen@mail.utoronto.ca"
    assert parsed["Auto-Submitted"] == "auto-replied"
    assert parsed["X-Auto-Response-Suppress"] == "All"
    assert parsed["X-Misty-Receipt-ID"] == str(rid)
    assert parsed["In-Reply-To"] == "<outer-123@mail.utoronto.ca>"
    assert parsed["References"] == "<outer-123@mail.utoronto.ca>"

    # Header injection & self-reply are rejected before calling Gmail.
    with pytest.raises(GmailError, match="self_reply_forbidden"):
        mb.send_reply(
            GmailReplyRequest(
                thread_id="thr_1",
                to_email=MISTY_ADDR,
                subject="Re: Test",
                body_text="Loop",
                receipt_id=uuid4(),
            )
        )
    with pytest.raises(GmailError, match="invalid_reply_header"):
        mb.send_reply(
            GmailReplyRequest(
                thread_id="thr_1",
                to_email="alex@utmist.ca",
                subject="Re: Test\r\nBcc: victim@example.com",
                body_text="Inject",
                receipt_id=uuid4(),
            )
        )

    # Receipt check finds sent message by X-Misty-Receipt-ID in thread metadata.
    gmail.threads["thr_1"] = {
        "id": "thr_1",
        "messages": [
            {
                "id": "sent_1",
                "payload": {"headers": [{"name": "X-Misty-Receipt-ID", "value": str(rid)}]},
            }
        ],
    }
    check = mb.check_receipt(GmailReceiptCheckRequest(thread_id="thr_1", receipt_id=rid))
    assert check.sent is True
    assert check.sent_message_id == "sent_1"
    missing = mb.check_receipt(GmailReceiptCheckRequest(thread_id="thr_1", receipt_id=uuid4()))
    assert missing.sent is False


@pytest.mark.parametrize(
    "status,code,uncertain",
    [
        (404, "gmail_not_found", False),
        (403, "gmail_403", False),
        (429, "gmail_429", True),
        (503, "gmail_503", True),
    ],
)
def test_gmail_errors_normalize_with_no_retries_and_no_upstream_body(status, code, uncertain):
    class Failing:
        def execute(self, **kwargs):
            assert kwargs == {"num_retries": 0}
            raise HttpError(
                httplib2.Response({"status": str(status)}), b"secret upstream email snippet"
            )

    with pytest.raises(GmailError) as exc_info:
        _execute(Failing())
    assert exc_info.value.code == code
    assert exc_info.value.uncertain is uncertain
    assert "secret upstream email snippet" not in str(exc_info.value)


def test_routes_enforce_scopes_hide_sensitive_422_inputs_and_omit_pii_from_audit_logs(caplog):
    gmail = FakeGmail()
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email()

    read_plain, read_prefix, read_hash = generate_key()
    send_plain, send_prefix, send_hash = generate_key()
    store = key_store_from_config(
        json.dumps(
            [
                {
                    "name": "docs-reader",
                    "prefix": read_prefix,
                    "key_hash": read_hash,
                    "scopes": ["email-read"],
                },
                {
                    "name": "docs-sender",
                    "prefix": send_prefix,
                    "key_hash": send_hash,
                    "scopes": ["email-send"],
                },
            ]
        )
    )

    app = create_app()
    app.dependency_overrides[get_gmail_mailbox] = lambda: build_mailbox(gmail)
    app.dependency_overrides[get_key_store] = lambda: store

    caplog.set_level(logging.INFO, logger="connectors.audit")
    with TestClient(app) as client:
        # Unauthenticated -> 401 before mailbox touch
        assert client.post("/google/gmail/messages/list", json={}).status_code == 401
        assert not gmail.list_calls

        # Scope separation: email-read cannot send; email-send cannot read
        assert (
            client.post(
                "/google/gmail/reply",
                headers={"X-API-Key": read_plain},
                json={
                    "thread_id": "thr_1",
                    "to_email": "alex@utmist.ca",
                    "subject": "Re: Hi",
                    "body_text": "Receipt",
                    "receipt_id": str(uuid4()),
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/google/gmail/messages/list",
                headers={"X-API-Key": send_plain},
                json={},
            ).status_code
            == 403
        )

        # Validation error hides input values (never echoes private email body/address)
        bad_resp = client.post(
            "/google/gmail/reply",
            headers={"X-API-Key": send_plain},
            json={
                "thread_id": "invalid spaces!",
                "to_email": "private-person@utoronto.ca",
                "subject": "Confidential Sponsor Terms",
                "body_text": "Super confidential email text",
                "receipt_id": str(uuid4()),
                "unexpected": 1,
            },
        )
        assert bad_resp.status_code == 422
        err_text = json.dumps(bad_resp.json())
        assert "private-person@utoronto.ca" not in err_text
        assert "Super confidential email text" not in err_text
        assert "Confidential Sponsor Terms" not in err_text

        # Authorized inspect succeeds and keeps sender/subject/body out of audit logs
        ok_resp = client.post(
            "/google/gmail/messages/msg_1",
            headers={"X-API-Key": read_plain},
        )
        assert ok_resp.status_code == 200
        assert ok_resp.json()["classification"] == "accepted_candidate"

    audit_blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "accepted_candidate" in audit_blob
    assert "alex.chen@mail.utoronto.ca" not in audit_blob
    assert "Sponsor Commitment" not in audit_blob
    assert "Gold tier" not in audit_blob


def test_unconfigured_gmail_mailbox_returns_503():
    app = create_app()
    app.dependency_overrides[get_gmail_mailbox] = lambda: GoogleGmailMailbox(address="")
    with TestClient(app) as client:
        resp = client.post("/google/gmail/messages/list", headers=AUTH, json={})
        assert resp.status_code == 503
        assert resp.json()["detail"] == {"code": "gmail_not_configured", "uncertain": False}


def test_attached_eml_and_multipart_attachments_are_pruned_before_body_extraction():
    """P1 regression: attached .eml text must never leak into forwarder_note or body."""
    outer = EmailMessage()
    outer["Authentication-Results"] = (
        "mx.google.com; dmarc=pass header.from=utoronto.ca; "
        "dkim=pass header.d=utoronto.ca; spf=pass"
    )
    outer["From"] = "Alex Chen <alex.chen@mail.utoronto.ca>"
    outer["To"] = MISTY_ADDR
    outer["Subject"] = "Fwd: Attached correspondence"
    outer["Message-ID"] = "<outer-eml-1@mail.utoronto.ca>"
    outer.set_content(
        "Archiving for my private notes only.\n\n"
        "---------- Forwarded message ---------\n"
        "From: External Partner <partner@acme.example>\n"
        "Subject: Proposal\n\n"
        "Original inline text."
    )

    attached_eml = EmailMessage()
    attached_eml["From"] = "attacker@evil.example"
    attached_eml["Subject"] = "Injected instructions"
    attached_eml.set_content("Share with finance team\n\n---------- Forwarded message ---------\n")
    outer.add_attachment(attached_eml, filename="forwarded.eml")

    # Also test an inline (non-attachment disposition) message/rfc822 embedded message.
    inline_eml = EmailMessage()
    inline_eml["From"] = "attacker2@evil.example"
    inline_eml["Subject"] = "Inline embedded"
    inline_eml.set_content("Share with ops team immediately")
    outer.add_attachment(inline_eml, disposition="inline")

    raw_bytes = outer.as_bytes()
    meta = {
        "id": "msg_eml",
        "threadId": "thr_eml",
        "labelIds": ["INBOX"],
        "internalDate": INTERNAL_MS,
        "sizeEstimate": len(raw_bytes),
        "payload": {"headers": [{"name": k, "value": v} for k, v in outer.items()]},
    }
    raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")

    gmail = FakeGmail()
    gmail.metadata["msg_eml"] = meta
    gmail.raw["msg_eml"] = {"id": "msg_eml", "threadId": "thr_eml", "raw": raw_b64}

    res = build_mailbox(gmail).inspect_message("msg_eml")
    assert res.classification == "accepted_candidate"
    assert res.forwarder_note == "Archiving for my private notes only."
    assert "Share with finance team" not in res.forwarder_note
    assert "Share with finance team" not in res.forwarded_body
    assert "Share with ops team" not in res.forwarder_note
    assert "Share with ops team" not in res.forwarded_body
    assert res.omitted_attachment_count == 2
    assert [a.content_type for a in res.omitted_attachments] == [
        "message/rfc822",
        "message/rfc822",
    ]
    assert all(a.size_bytes > 0 for a in res.omitted_attachments)
    assert "attachments omitted" in res.warnings


def test_attached_eml_with_empty_outer_body_does_not_become_forwarder_note():
    """Exact reviewer repro: empty outer text + .eml containing delimiter-free text."""
    outer = EmailMessage()
    outer["Authentication-Results"] = "mx.google.com; dmarc=pass header.from=utmist.ca"
    outer["From"] = "member@utmist.ca"
    outer["To"] = MISTY_ADDR
    outer["Subject"] = "Fwd: Finance"
    outer["Message-ID"] = "<outer-eml-2@utmist.ca>"
    outer.make_mixed()

    attached_eml = EmailMessage()
    attached_eml["From"] = "external@other.example"
    attached_eml["Subject"] = "Inner"
    attached_eml.set_content(
        "Share with finance team\n\n---------- Forwarded message ---------\nInner body."
    )
    outer.add_attachment(attached_eml, filename="inner.eml")

    raw_bytes = outer.as_bytes()
    meta = {
        "id": "msg_eml2",
        "threadId": "thr_eml2",
        "labelIds": ["INBOX"],
        "internalDate": INTERNAL_MS,
        "sizeEstimate": len(raw_bytes),
        "payload": {"headers": [{"name": k, "value": v} for k, v in outer.items()]},
    }
    raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")

    gmail = FakeGmail()
    gmail.metadata["msg_eml2"] = meta
    gmail.raw["msg_eml2"] = {"id": "msg_eml2", "threadId": "thr_eml2", "raw": raw_b64}

    res = build_mailbox(gmail).inspect_message("msg_eml2")
    assert res.classification == "accepted_candidate"
    assert res.forwarder_note == ""
    assert res.forwarded_body == ""
    assert res.omitted_attachment_count == 1
    assert res.omitted_attachments[0].content_type == "message/rfc822"


def test_unknown_mime_charset_falls_back_to_utf8_with_warning():
    raw_bytes = (
        b"Authentication-Results: mx.google.com; dmarc=pass header.from=utmist.ca\r\n"
        b"From: member@utmist.ca\r\n"
        b"To: misty@utmist.ca\r\n"
        b"Subject: Fwd: Unknown charset\r\n"
        b"Message-ID: <charset-1@utmist.ca>\r\n"
        b'Content-Type: text/plain; charset="x-bogus-charset"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n"
        b"Hello from unknown charset \xff body.\r\n"
    )
    parsed = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    meta = {
        "id": "msg_charset",
        "threadId": "thr_charset",
        "labelIds": ["INBOX"],
        "internalDate": INTERNAL_MS,
        "sizeEstimate": len(raw_bytes),
        "payload": {"headers": [{"name": k, "value": v} for k, v in parsed.items()]},
    }
    raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")

    gmail = FakeGmail()
    gmail.metadata["msg_charset"] = meta
    gmail.raw["msg_charset"] = {"id": "msg_charset", "threadId": "thr_charset", "raw": raw_b64}

    res = build_mailbox(gmail).inspect_message("msg_charset")
    assert res.classification == "accepted_candidate"
    assert "Hello from unknown charset" in res.forwarded_body
    assert "unknown charset fallback" in res.warnings


@pytest.mark.parametrize(
    "from_addr,dkim_domain,expected",
    [
        # Sibling subdomains under same org domain
        ("member@mail.example.com", "send.example.com", "accepted_candidate"),
        # Child signing domain for parent From domain
        ("member@example.com", "mail.example.com", "accepted_candidate"),
        # Parent signing domain for child From domain
        ("member@mail.example.com", "example.com", "accepted_candidate"),
        # Siblings under multi-label public suffix
        ("member@mail.acme.co.uk", "send.acme.co.uk", "accepted_candidate"),
        # Distinct org domains under multi-label public suffix must NOT align
        ("member@a.co.uk", "b.co.uk", "rejected_unauthenticated"),
        # Distinct org domains under Canadian provincial suffix must NOT align
        ("member@a.on.ca", "b.on.ca", "rejected_unauthenticated"),
        # Distinct multi-tenant hosting domains must NOT align
        ("member@tenant-a.github.io", "tenant-b.github.io", "rejected_unauthenticated"),
    ],
)
def test_relaxed_dkim_organizational_domain_alignment(from_addr, dkim_domain, expected):
    gmail = FakeGmail()
    gmail.metadata["msg_1"], gmail.raw["msg_1"] = make_raw_email(
        from_headers=(from_addr,),
        auth_headers=(
            f"mx.google.com; dkim=pass header.d={dkim_domain} header.s=sel1; spf=neutral; dmarc=none",
        ),
    )
    res = build_mailbox(gmail).inspect_message("msg_1")
    assert res.classification == expected
    if expected == "accepted_candidate":
        assert res.auth_mechanism == "dkim_aligned_no_dmarc"


def test_receipt_check_recovers_sent_receipt_from_another_thread_and_fails_closed_on_cap():
    gmail = FakeGmail()
    mb = build_mailbox(gmail)
    rid = uuid4()

    # Original thread does NOT contain the receipt (e.g., Gmail placed reply in a new thread).
    gmail.threads["thr_orig"] = {"id": "thr_orig", "messages": [{"id": "msg_in", "payload": {}}]}
    gmail.list_response = {
        "messages": [{"id": "sent_other_thread", "threadId": "thr_other"}],
    }
    gmail.metadata["sent_other_thread"] = {
        "id": "sent_other_thread",
        "payload": {"headers": [{"name": "X-Misty-Receipt-ID", "value": str(rid)}]},
    }

    found = mb.check_receipt(
        GmailReceiptCheckRequest(
            thread_id="thr_orig", receipt_id=rid, sent_after_epoch_s=1700000000
        )
    )
    assert found.sent is True
    assert found.sent_message_id == "sent_other_thread"
    assert gmail.list_calls[-1]["labelIds"] == ["SENT"]
    assert gmail.list_calls[-1]["q"] == "after:1700000000"

    # Also recovers when original thread returns 404.
    def raise_404():
        raise HttpError(httplib2.Response({"status": "404"}), b"not found")

    gmail.threads["thr_missing"] = raise_404
    found_missing_thr = mb.check_receipt(
        GmailReceiptCheckRequest(thread_id="thr_missing", receipt_id=rid)
    )
    assert found_missing_thr.sent is True
    assert found_missing_thr.sent_message_id == "sent_other_thread"

    # Truncated SENT scan (more pages beyond cap) fails closed as uncertain error, never sent=False.
    page_counter = {"n": 0}

    def endless_pages(_kwargs):
        page_counter["n"] += 1
        return {"messages": [], "nextPageToken": f"tok_{page_counter['n']}"}

    gmail.list_response = endless_pages
    gmail.threads["thr_orig"] = {"id": "thr_orig", "messages": []}
    with pytest.raises(GmailError) as exc_info:
        mb.check_receipt(GmailReceiptCheckRequest(thread_id="thr_orig", receipt_id=uuid4()))
    assert exc_info.value.code == "receipt_check_inconclusive"
    assert exc_info.value.uncertain is True


def test_http_422_responses_strip_private_input_on_overlong_and_malformed_requests():
    gmail = FakeGmail()
    app = create_app()
    app.dependency_overrides[get_gmail_mailbox] = lambda: build_mailbox(gmail)

    secret_body = "PRIVATE-SENSITIVE-BODY-" + ("x" * 50_050)
    secret_addr = "private-overlong-" + ("a" * 330) + "@utoronto.ca"
    secret_subj = "CONFIDENTIAL-SUBJECT-" + ("s" * 520)

    with TestClient(app) as client:
        # Overlong sensitive fields directly fail field validation on FastAPI's 422 handler.
        resp = client.post(
            "/google/gmail/reply",
            headers=AUTH,
            json={
                "thread_id": "thr_1",
                "to_email": secret_addr,
                "subject": secret_subj,
                "body_text": secret_body,
                "receipt_id": str(uuid4()),
            },
        )
        assert resp.status_code == 422
        payload = resp.json()
        assert all("input" not in err for err in payload["detail"])
        body_dump = json.dumps(payload)
        assert "PRIVATE-SENSITIVE-BODY-" not in body_dump
        assert "private-overlong-" not in body_dump
        assert "CONFIDENTIAL-SUBJECT-" not in body_dump

        # Malformed JSON must not echo raw request bytes either.
        bad_json = client.post(
            "/google/gmail/reply",
            headers={**AUTH, "Content-Type": "application/json"},
            content='{"body_text": "SECRET-BROKEN-JSON-PAYLOAD"',
        )
        assert bad_json.status_code == 422
        assert "SECRET-BROKEN-JSON-PAYLOAD" not in bad_json.text
