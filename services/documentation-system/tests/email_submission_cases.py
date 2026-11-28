"""Shared forwarded-email intake and receipt parity cases (in-memory and Postgres)."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from contracts.directory import DirectoryUnavailable, VerifiedMember
from contracts.email import (
    ClaimedHeaders,
    GmailError,
    GmailInspectResult,
    GmailListPage,
    GmailMessageRef,
    OmittedAttachment,
)
from src.config import Settings
from src.email_submissions import EmailSubmissions

MAILBOX = "misty@utmist.ca"
ALICE_ID = UUID("11111111-1111-1111-1111-111111111111")
BOB_ID = UUID("22222222-2222-2222-2222-222222222222")
RETIRED_ID = UUID("33333333-3333-3333-3333-333333333333")


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 27, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> datetime:
        self.t = self.t + timedelta(**kwargs)
        return self.t


class FakeDirectory:
    def __init__(self) -> None:
        self.down = False
        self.members = {
            "alice@utmist.ca": VerifiedMember(ALICE_ID, "Alice Chen", "alice@utmist.ca"),
            "alice.alias@mail.utoronto.ca": VerifiedMember(
                ALICE_ID, "Alice Chen", "alice@utmist.ca"
            ),
            "bob@utmist.ca": VerifiedMember(BOB_ID, "Bob Li", "bob@utmist.ca"),
        }

    def get_active_member_by_verified_email(self, email: str) -> VerifiedMember | None:
        if self.down:
            raise DirectoryUnavailable("directory down")
        return self.members.get(email.strip().lower())


class FakeGmailClient:
    def __init__(self, mailbox: str = MAILBOX) -> None:
        self.mailbox = mailbox
        self.messages: dict[str, GmailInspectResult] = {}
        self.order: list[str] = []
        self.sent_replies: list[dict] = []
        self.check_calls: list[dict] = []
        self.receipt_lookup: dict[tuple[str, UUID], str] = {}
        self.fail_send: GmailError | None = None
        self.fail_check: GmailError | None = None
        self.fail_inspect: set[str] = set()

    def add(self, inspected: GmailInspectResult) -> None:
        self.messages[inspected.message_id] = inspected
        if inspected.message_id not in self.order:
            self.order.append(inspected.message_id)

    def list_messages(self, *, after_epoch_s, page_token, page_size) -> GmailListPage:
        eligible = [
            mid
            for mid in self.order
            if after_epoch_s is None
            or int(self.messages[mid].received_at.timestamp()) >= after_epoch_s
        ]
        start = int(page_token) if page_token is not None else 0
        chunk = eligible[start : start + page_size]
        next_tok = str(start + page_size) if start + page_size < len(eligible) else None
        return GmailListPage(
            mailbox_address=self.mailbox,
            messages=[
                GmailMessageRef(message_id=mid, thread_id=self.messages[mid].thread_id)
                for mid in chunk
            ],
            next_page_token=next_tok,
        )

    def inspect_message(self, message_id: str) -> GmailInspectResult:
        if message_id in self.fail_inspect:
            raise GmailError("gmail_503", uncertain=True)
        return self.messages[message_id]

    def send_reply(
        self, *, thread_id, to_email, subject, body_text, in_reply_to, receipt_id
    ) -> str:
        if self.fail_send is not None:
            raise self.fail_send
        sent_id = f"sent_{len(self.sent_replies) + 1}"
        record = {
            "sent_id": sent_id,
            "thread_id": thread_id,
            "to_email": to_email,
            "subject": subject,
            "body_text": body_text,
            "in_reply_to": in_reply_to,
            "receipt_id": receipt_id,
        }
        self.sent_replies.append(record)
        self.receipt_lookup[(thread_id, receipt_id)] = sent_id
        return sent_id

    def check_receipt(self, *, thread_id, receipt_id, sent_after_epoch_s=None) -> str | None:
        self.check_calls.append(
            {
                "thread_id": thread_id,
                "receipt_id": receipt_id,
                "sent_after_epoch_s": sent_after_epoch_s,
            }
        )
        if self.fail_check is not None:
            raise self.fail_check
        return self.receipt_lookup.get((thread_id, receipt_id))


def make_candidate(
    message_id: str = "msg_1",
    *,
    thread_id: str = "thr_1",
    sender: str = "alice.alias@mail.utoronto.ca",
    outer_message_id: str | None = "<outer-1@utoronto.ca>",
    subject: str = "Fwd: Venue Contract",
    classification: str = "accepted_candidate",
    received_at: datetime | None = None,
) -> GmailInspectResult:
    now = received_at or datetime(2026, 11, 27, 3, 55, tzinfo=timezone.utc)
    if classification in {"ignored_non_inbox", "ignored_loop", "rejected_unauthenticated"}:
        return GmailInspectResult(
            classification=classification,
            mailbox_address=MAILBOX,
            message_id=message_id,
            thread_id=thread_id,
            received_at=now,
        )
    if classification == "rejected_oversize":
        return GmailInspectResult(
            classification="rejected_oversize",
            mailbox_address=MAILBOX,
            message_id=message_id,
            thread_id=thread_id,
            received_at=now,
            verified_sender_email=sender,
            auth_mechanism="dmarc_pass",
            outer_message_id=outer_message_id,
            subject=subject,
        )
    return GmailInspectResult(
        classification="accepted_candidate",
        mailbox_address=MAILBOX,
        message_id=message_id,
        thread_id=thread_id,
        received_at=now,
        verified_sender_email=sender,
        auth_mechanism="dmarc_pass",
        outer_message_id=outer_message_id,
        subject=subject,
        forwarder_note="Keep for logistics.",
        forwarded_body="---------- Forwarded message ---------\nRoom booked for Friday.",
        delimiter_detected=True,
        truncated=False,
        omitted_attachment_count=1,
        omitted_attachments=[OmittedAttachment(content_type="application/pdf", size_bytes=1200)],
        untrusted_claimed_headers=ClaimedHeaders(
            from_raw="External Host <host@venue.example>",
            subject_raw="Venue Contract",
        ),
        warnings=["attachments omitted"],
    )


class EmailSubmissionCases:
    @pytest.fixture
    def email_flow(self, adapter):
        clock = Clock()
        gmail = FakeGmailClient()
        directory = FakeDirectory()
        settings = Settings(
            _env_file=None,
            email_mailbox_identity=MAILBOX,
            email_receipt_member_hourly_limit=5,
            email_receipt_global_hourly_limit=30,
            email_receipt_max_attempts=3,
        )
        svc = EmailSubmissions(adapter, gmail, directory, settings, now=clock)
        return svc, gmail, directory, clock

    def test_verified_alias_accepted_separates_provenance_and_sends_threaded_receipt(
        self, email_flow
    ):
        svc, gmail, _, _ = email_flow
        gmail.add(make_candidate("msg_alias", sender="alice.alias@mail.utoronto.ca"))

        res = svc.poll(actor="email-poller")
        assert res.accepted == 1 and res.receipts_sent == 1 and res.complete is True
        assert svc.storage.get_email_inbox_watermark(MAILBOX) is not None

        subs = svc.storage.list_email_submissions(submitter_person_id=ALICE_ID)
        assert len(subs) == 1
        sub = subs[0]
        assert sub.submitter_person_id == ALICE_ID
        assert sub.submitter_label == "Alice Chen"
        assert sub.receipt_status == "sent"
        assert sub.pending_payload["forwarder_note"] == "Keep for logistics."
        assert (
            sub.pending_payload["untrusted_claimed_headers"]["from_raw"]
            == "External Host <host@venue.example>"
        )
        pub = sub.public()
        assert "pending_payload" not in pub
        assert "verified_sender_email" not in pub
        assert "outer_message_id" not in pub

        # Receipt replies to the verified alias thread, never the quoted author.
        assert len(gmail.sent_replies) == 1
        reply = gmail.sent_replies[0]
        assert reply["to_email"] == "alice.alias@mail.utoronto.ca"
        assert reply["in_reply_to"] == "<outer-1@utoronto.ca>"
        assert str(sub.submission_id) in reply["body_text"]

    def test_unauthenticated_loop_and_unknown_senders_produce_zero_backscatter(self, email_flow):
        svc, gmail, _, _ = email_flow
        gmail.add(make_candidate("m_spoof", classification="rejected_unauthenticated"))
        gmail.add(make_candidate("m_loop", classification="ignored_loop"))
        gmail.add(make_candidate("m_spam", classification="ignored_non_inbox"))
        gmail.add(make_candidate("m_stranger", sender="stranger@external.example"))

        res = svc.poll(actor="email-poller")
        assert res.rejected_unauthenticated == 1
        assert res.ignored_loop == 1
        assert res.ignored_non_inbox == 1
        assert res.ignored_unknown_sender == 1
        assert res.accepted == 0
        assert gmail.sent_replies == []
        assert svc.storage.list_email_submissions() == []

    def test_deduplicates_retries_while_keeping_separate_members_forwards_independent(
        self, email_flow
    ):
        svc, gmail, _, _ = email_flow
        # 1. Alice forwards message; MTA also delivers a duplicate copy with same outer Message-ID.
        gmail.add(
            make_candidate(
                "msg_alice_1",
                thread_id="thr_a1",
                sender="alice@utmist.ca",
                outer_message_id="<same-fwd@utmist.ca>",
            )
        )
        gmail.add(
            make_candidate(
                "msg_alice_dup",
                thread_id="thr_a1",
                sender="alice@utmist.ca",
                outer_message_id="<same-fwd@utmist.ca>",
            )
        )
        # 2. Bob forwards the exact same underlying correspondence (even with an identical outer ID).
        gmail.add(
            make_candidate(
                "msg_bob_1",
                thread_id="thr_b1",
                sender="bob@utmist.ca",
                outer_message_id="<same-fwd@utmist.ca>",
            )
        )

        first = svc.poll(actor="email-poller")
        assert first.accepted == 2
        assert first.duplicates == 1
        assert len(gmail.sent_replies) == 2

        # Re-polling the exact same inbox messages skips all three cleanly.
        second = svc.poll(actor="email-poller")
        assert second.accepted == 0 and second.duplicates == 0
        assert len(svc.storage.list_email_submissions()) == 2
        owners = {s.submitter_person_id for s in svc.storage.list_email_submissions()}
        assert owners == {ALICE_ID, BOB_ID}

    def test_directory_outage_defers_message_without_dropping_it(self, email_flow):
        svc, gmail, directory, clock = email_flow
        gmail.add(
            make_candidate(
                "msg_defer",
                thread_id="thr_d",
                sender="alice@utmist.ca",
                received_at=clock() - timedelta(hours=2),
            )
        )
        directory.down = True
        t0 = clock()

        deferred = svc.poll(actor="email-poller")
        assert deferred.directory_deferred == 1
        assert deferred.complete is False
        # Initial lower bound persisted before processing so recovery >24h later still sees it.
        initial_wm = svc.storage.get_email_inbox_watermark(MAILBOX)
        assert initial_wm == t0 - timedelta(hours=svc.settings.email_initial_lookback_hours)
        assert not svc.storage.is_email_message_processed(MAILBOX, "msg_defer")

        # Directory recovers 25 hours later (> 24h initial lookback); message must still drain.
        clock.advance(hours=25)
        directory.down = False
        recovered = svc.poll(actor="email-poller")
        assert recovered.accepted == 1 and recovered.complete is True
        assert svc.storage.is_email_message_processed(MAILBOX, "msg_defer")
        assert svc.storage.get_email_inbox_watermark(MAILBOX) == clock()

    def test_backlog_larger_than_one_scan_window_drains_across_batches(self, email_flow):
        svc, gmail, _, clock = email_flow
        # Pre-seed 125 already-processed messages + 1 unseen message #126 (5 pages of 25 + 1).
        for idx in range(126):
            mid = f"msg_backlog_{idx:03d}"
            gmail.add(
                make_candidate(
                    mid,
                    thread_id=f"thr_b_{idx:03d}",
                    sender="alice@utmist.ca",
                    outer_message_id=f"<backlog-{idx}@utmist.ca>",
                )
            )
            if idx < 125:
                svc.storage.record_processed_email_message(
                    MAILBOX, mid, outcome="accepted", processed_at=clock()
                )

        t0 = clock()
        batch1 = svc.poll(limit=25, max_pages=5, actor="email-poller")
        assert batch1.listed == 125
        assert batch1.accepted == 0
        assert batch1.complete is False
        assert batch1.next_page_token == "125"
        # Watermark stays at initial lower bound until the full backlog finishes.
        assert svc.storage.get_email_inbox_watermark(MAILBOX) == t0 - timedelta(
            hours=svc.settings.email_initial_lookback_hours
        )

        batch2 = svc.poll(
            limit=25,
            max_pages=5,
            page_token=batch1.next_page_token,
            actor="email-poller",
        )
        assert batch2.listed == 1
        assert batch2.accepted == 1
        assert batch2.complete is True
        assert batch2.next_page_token is None
        assert svc.storage.is_email_message_processed(MAILBOX, "msg_backlog_125")
        assert svc.storage.get_email_inbox_watermark(MAILBOX) == t0

    def test_failure_notice_preserves_submission_last_error_across_retries_and_success(
        self, email_flow
    ):
        svc, gmail, _, clock = email_flow
        gmail.add(
            make_candidate(
                "msg_fail_keep",
                thread_id="thr_fk",
                sender="bob@utmist.ca",
                outer_message_id="<fk@utmist.ca>",
            )
        )
        svc.poll(actor="email-poller")
        sub = svc.storage.list_email_submissions(submitter_person_id=BOB_ID)[0]

        # First failure-notice send fails with a definite Gmail error; submission last_error stays intact.
        gmail.fail_send = GmailError("gmail_403", uncertain=False)
        failed = svc.mark_failed(sub.submission_id, error_code="doc_writer_denied", actor="admin")
        assert failed is not None
        assert failed.status == "failed"
        assert failed.last_error == "doc_writer_denied"
        assert failed.notification_last_error == "gmail_403"
        assert failed.failure_notice_status == "pending"

        # Retry succeeds: notification_last_error clears, last_error stays "doc_writer_denied",
        # and the delivered failure notice reports the real processing error.
        gmail.fail_send = None
        clock.advance(minutes=2)
        svc.poll(actor="email-poller")
        after = svc.storage.get_email_submission(sub.submission_id)
        assert after is not None
        assert after.failure_notice_status == "sent"
        assert after.last_error == "doc_writer_denied"
        assert after.notification_last_error is None
        assert "doc_writer_denied" in gmail.sent_replies[-1]["body_text"]

    def test_uncertain_receipt_reconciles_existing_send_without_duplicate_email(self, email_flow):
        svc, gmail, _, clock = email_flow
        gmail.add(make_candidate("msg_uncertain", thread_id="thr_u", sender="alice@utmist.ca"))
        gmail.fail_send = GmailError("gmail_503", uncertain=True)

        first = svc.poll(actor="email-poller")
        assert first.accepted == 1 and first.receipts_failed == 1
        sub = svc.storage.list_email_submissions()[0]
        assert sub.receipt_status == "uncertain"
        assert sub.receipt_attempt_id is not None

        # Simulate that Gmail actually committed the message before the 503 timeout!
        gmail.receipt_lookup[("thr_u", sub.receipt_attempt_id)] = "sent_reconciled"
        gmail.fail_send = None
        clock.advance(minutes=2)

        retried = svc.poll(actor="email-poller")
        assert retried.receipts_sent == 1
        assert gmail.sent_replies == []  # Never sent a duplicate!
        updated = svc.storage.get_email_submission(sub.submission_id)
        assert updated.receipt_status == "sent"
        assert updated.receipt_gmail_message_id == "sent_reconciled"

    def test_rate_limit_suppresses_excess_receipts_while_retaining_submissions(self, email_flow):
        svc, gmail, _, _ = email_flow
        for idx in range(6):
            gmail.add(
                make_candidate(
                    f"msg_{idx}",
                    thread_id=f"thr_{idx}",
                    sender="alice@utmist.ca",
                    outer_message_id=f"<fwd-{idx}@utmist.ca>",
                )
            )
        res = svc.poll(actor="email-poller")
        assert res.accepted == 6
        assert res.receipts_sent == 5
        assert res.receipts_suppressed == 1
        statuses = [s.receipt_status for s in svc.storage.list_email_submissions()]
        assert statuses.count("sent") == 5
        assert statuses.count("suppressed_rate_limit") == 1

    def test_oversize_failure_notice_and_retention_pruning(self, email_flow):
        svc, gmail, _, clock = email_flow
        gmail.add(
            make_candidate(
                "msg_big",
                thread_id="thr_big",
                sender="alice@utmist.ca",
                outer_message_id="<big@utmist.ca>",
                classification="rejected_oversize",
            )
        )
        gmail.add(
            make_candidate(
                "msg_ok",
                thread_id="thr_ok",
                sender="bob@utmist.ca",
                outer_message_id="<ok@utmist.ca>",
            )
        )
        res = svc.poll(actor="email-poller")
        assert res.rejected_oversize == 1 and res.accepted == 1
        big = svc.storage.list_email_submissions(submitter_person_id=ALICE_ID)[0]
        assert big.status == "rejected_oversize" and big.pending_payload is None
        assert "5 MiB" in gmail.sent_replies[0]["body_text"]

        ok = svc.storage.list_email_submissions(submitter_person_id=BOB_ID)[0]
        failed = svc.mark_failed(ok.submission_id, error_code="doc_create_failed", actor="admin")
        assert failed.status == "failed" and failed.failure_notice_status == "sent"
        assert failed.pending_payload is not None

        # After 8 days (> 7d failed payload window), pending_payload is cleared while metadata stays.
        clock.advance(days=8)
        svc.poll(actor="email-poller")
        after_7d = svc.storage.get_email_submission(ok.submission_id)
        assert after_7d is not None and after_7d.pending_payload is None

        # After > 365d total retention, records and processed markers are pruned.
        clock.advance(days=360)
        svc.poll(actor="email-poller")
        assert svc.storage.list_email_submissions() == []
        assert not svc.storage.is_email_message_processed(MAILBOX, "msg_ok")

    def test_poll_run_guard_is_exclusive_and_owner_checked(self, adapter):
        first, second = uuid4(), uuid4()
        assert adapter.acquire_email_poll_run(first, actor="runner-1") is True
        assert adapter.acquire_email_poll_run(first, actor="runner-1") is False
        assert adapter.acquire_email_poll_run(second, actor="runner-2") is False
        assert adapter.release_email_poll_run(second) is False
        assert adapter.release_email_poll_run(first) is True
        assert adapter.acquire_email_poll_run(second, actor="runner-2") is True
        assert adapter.release_email_poll_run(second) is True
