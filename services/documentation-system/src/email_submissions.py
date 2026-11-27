"""Durable forwarded-email intake, active-member binding, and receipt state machine."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from contracts.directory import DirectoryClient, DirectoryUnavailable
from contracts.email import (
    EmailPollResult,
    EmailSubmission,
    GmailClient,
    GmailError,
    GmailInspectResult,
)
from contracts.storage import StorageAdapter


def _reply_subject(subject: str | None) -> str:
    base = (subject or "Forwarded email").strip()
    if base.lower().startswith("re:"):
        return base[:500]
    return f"Re: {base}"[:500]


def _render_receipt_body(submission: EmailSubmission) -> str:
    if submission.status == "rejected_oversize":
        return (
            "Misty could not accept your forwarded email because it exceeds the 5 MiB "
            "size limit.\n\n"
            f"Submission ID: {submission.submission_id}\n"
            f"Received at (UTC): {submission.received_at.isoformat()}\n"
            "Status: rejected_oversize\n\n"
            "Please forward a smaller message without large attachments."
        )

    lines = [
        "Misty received your forwarded email and recorded a durable receipt.",
        "",
        f"Submission ID: {submission.submission_id}",
        f"Received at (UTC): {submission.received_at.isoformat()}",
        f"Owner: {submission.submitter_label or submission.submitter_person_id}",
        f"Status: {submission.status} (persistence: {submission.persistence_status})",
    ]
    if submission.omitted_attachment_count:
        lines.append(
            f"Note: {submission.omitted_attachment_count} attachment(s) were omitted "
            "(message text only in this slice)."
        )
    if submission.truncated:
        lines.append("Note: Extracted message text was truncated to the 500,000-character limit.")
    lines.extend(
        [
            "",
            "This submission is private to your verified Misty identity by default. "
            "Quoted headers and forwarded text never grant team or channel access.",
        ]
    )
    return "\n".join(lines)


def _render_failure_body(submission: EmailSubmission) -> str:
    return (
        "Misty could not finish processing your forwarded email after retries.\n\n"
        f"Submission ID: {submission.submission_id}\n"
        f"Received at (UTC): {submission.received_at.isoformat()}\n"
        f"Status: failed ({submission.last_error or 'processing_failed'})\n\n"
        "You may forward the message again or contact a Misty admin with the Submission ID above."
    )


def _backoff_seconds(attempts: int) -> int:
    return min(3600, 60 * (2 ** max(0, attempts - 1)))


class EmailSubmissions:
    def __init__(
        self,
        storage: StorageAdapter,
        gmail: GmailClient,
        directory: DirectoryClient,
        settings,
        *,
        now=None,
    ) -> None:
        self.storage = storage
        self.gmail = gmail
        self.directory = directory
        self.settings = settings
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _expected_mailbox(self) -> str:
        return self.settings.email_mailbox_identity.strip().lower()

    def poll(
        self,
        *,
        limit: int = 25,
        max_pages: int = 5,
        actor: str,
        disconnected=None,
    ) -> EmailPollResult:
        now = self.now()
        cleaned = self.storage.prune_expired_email_records(
            now=now,
            failed_payload_days=self.settings.email_failed_payload_days,
            retention_days=self.settings.email_submission_retention_days,
            actor=actor,
        )

        expected_mb = self._expected_mailbox()
        watermark = self.storage.get_email_inbox_watermark(expected_mb) if expected_mb else None
        if watermark is not None:
            after_dt = watermark - timedelta(hours=self.settings.email_watermark_overlap_hours)
        else:
            after_dt = now - timedelta(hours=self.settings.email_initial_lookback_hours)
        after_epoch = max(0, int(after_dt.timestamp()))

        counts: dict[str, int] = {
            "listed": 0,
            "accepted": 0,
            "duplicates": 0,
            "rejected_oversize": 0,
            "ignored_non_inbox": 0,
            "ignored_loop": 0,
            "rejected_unauthenticated": 0,
            "ignored_unknown_sender": 0,
            "directory_deferred": 0,
            "inspect_failed": 0,
            "receipts_attempted": 0,
            "receipts_sent": 0,
            "receipts_suppressed": 0,
            "receipts_failed": 0,
            "expired_cleaned": cleaned,
        }

        page_token: str | None = None
        seen_tokens: set[str] = set()
        inspected_unseen = 0
        pages = 0
        hit_budget = False
        active_mailbox: str | None = expected_mb or None

        while pages < max_pages and inspected_unseen < limit:
            if disconnected and disconnected():
                hit_budget = True
                break
            page = self.gmail.list_messages(
                after_epoch_s=after_epoch,
                page_token=page_token,
                page_size=min(100, max(10, limit)),
            )
            pages += 1
            mailbox = page.mailbox_address.strip().lower()
            if expected_mb and mailbox != expected_mb:
                raise GmailError("mailbox_mismatch")
            if active_mailbox is None:
                active_mailbox = mailbox
                existing_wm = self.storage.get_email_inbox_watermark(mailbox)
                if existing_wm is not None and watermark is None:
                    watermark = existing_wm

            counts["listed"] += len(page.messages)
            for ref in page.messages:
                if inspected_unseen >= limit:
                    hit_budget = True
                    break
                if disconnected and disconnected():
                    hit_budget = True
                    break
                if self.storage.is_email_message_processed(mailbox, ref.message_id):
                    continue
                inspected_unseen += 1
                self._process_one(mailbox, ref.message_id, counts=counts, actor=actor)

            if hit_budget or page.next_page_token is None:
                if page.next_page_token is not None:
                    hit_budget = True
                break
            if page.next_page_token in seen_tokens:
                raise GmailError("gmail_repeated_page_token")
            seen_tokens.add(page.next_page_token)
            page_token = page.next_page_token
        else:
            hit_budget = True

        all_clean = (
            not hit_budget and counts["directory_deferred"] == 0 and counts["inspect_failed"] == 0
        )
        if all_clean and active_mailbox is not None:
            self.storage.set_email_inbox_watermark(active_mailbox, now, actor=actor)

        self.retry_due_notifications(limit=limit, actor=actor, counts=counts)
        return EmailPollResult(**counts, complete=all_clean)

    def _process_one(
        self,
        mailbox: str,
        message_id: str,
        *,
        counts: dict[str, int],
        actor: str,
    ) -> None:
        try:
            inspected = self.gmail.inspect_message(message_id)
        except GmailError:
            counts["inspect_failed"] += 1
            return

        if inspected.mailbox_address.strip().lower() != mailbox:
            counts["inspect_failed"] += 1
            return

        now = self.now()
        if inspected.received_at <= now - timedelta(
            days=self.settings.email_submission_retention_days
        ):
            # Gmail `after:` queries match at thread granularity; never re-ingest
            # an old message in an active thread past the retention horizon.
            return
        cls = inspected.classification
        if cls in {"ignored_non_inbox", "ignored_loop", "rejected_unauthenticated"}:
            self.storage.record_processed_email_message(
                mailbox, message_id, outcome=cls, processed_at=now
            )
            counts[cls] += 1
            return

        sender = (inspected.verified_sender_email or "").strip().lower()
        if not sender or inspected.auth_mechanism is None:
            self.storage.record_processed_email_message(
                mailbox,
                message_id,
                outcome="rejected_unauthenticated",
                processed_at=now,
            )
            counts["rejected_unauthenticated"] += 1
            return

        try:
            member = self.directory.get_active_member_by_verified_email(sender)
        except DirectoryUnavailable:
            # Keep message unprocessed so transient directory outages retry cleanly.
            counts["directory_deferred"] += 1
            return

        if member is None:
            # Unknown or inactive member: record and send ZERO outbound email (no backscatter).
            self.storage.record_processed_email_message(
                mailbox,
                message_id,
                outcome="ignored_unknown_sender",
                processed_at=now,
            )
            counts["ignored_unknown_sender"] += 1
            return

        submission = self._build_submission(mailbox, inspected, member, now=now, actor=actor)
        stored, created = self.storage.put_email_submission(submission)
        if not created:
            self.storage.record_processed_email_message(
                mailbox,
                message_id,
                outcome="duplicate_submission",
                processed_at=now,
                submission_id=stored.submission_id,
            )
            counts["duplicates"] += 1
            return

        outcome = "rejected_oversize" if stored.status == "rejected_oversize" else "accepted"
        self.storage.record_processed_email_message(
            mailbox,
            message_id,
            outcome=outcome,
            processed_at=now,
            submission_id=stored.submission_id,
        )
        counts[outcome] += 1
        self._advance_slot(stored.submission_id, kind="receipt", actor=actor, counts=counts)

    def _build_submission(
        self,
        mailbox: str,
        inspected: GmailInspectResult,
        member,
        *,
        now: datetime,
        actor: str,
    ) -> EmailSubmission:
        is_oversize = inspected.classification == "rejected_oversize"
        payload = (
            None
            if is_oversize
            else {
                "forwarder_note": inspected.forwarder_note,
                "forwarded_body": inspected.forwarded_body,
                "omitted_attachments": [
                    a.model_dump(mode="json") for a in inspected.omitted_attachments
                ],
                "untrusted_claimed_headers": inspected.untrusted_claimed_headers.model_dump(
                    mode="json"
                ),
            }
        )
        return EmailSubmission(
            submission_id=uuid4(),
            mailbox_identity=mailbox,
            gmail_message_id=inspected.message_id,
            gmail_thread_id=inspected.thread_id,
            outer_message_id=inspected.outer_message_id,
            submitter_person_id=member.person_id,
            submitter_label=member.display_name,
            verified_sender_email=inspected.verified_sender_email.strip().lower(),
            auth_mechanism=inspected.auth_mechanism,
            received_at=inspected.received_at,
            subject=inspected.subject,
            status="rejected_oversize" if is_oversize else "accepted",
            persistence_status="not_applicable" if is_oversize else "pending",
            delimiter_detected=inspected.delimiter_detected,
            truncated=inspected.truncated,
            omitted_attachment_count=inspected.omitted_attachment_count,
            warnings=list(inspected.warnings),
            pending_payload=payload,
            receipt_status="pending",
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
            created_by=actor,
            updated_by=actor,
        )

    def mark_failed(
        self,
        submission_id: UUID,
        *,
        error_code: str,
        actor: str,
    ) -> EmailSubmission | None:
        current = self.storage.get_email_submission(submission_id)
        if current is None:
            return None
        if current.status == "rejected_oversize":
            raise ValueError("rejected_oversize submission cannot be marked failed")
        now = self.now()
        if current.status == "failed":
            return current
        values = {
            "status": "failed",
            "persistence_status": "failed",
            "failed_at": now,
            "last_error": error_code,
            "failure_notice_status": (
                "pending"
                if current.failure_notice_status == "none"
                else current.failure_notice_status
            ),
            "next_attempt_at": now,
        }
        updated = self.storage.change_email_submission(current, values, actor=actor, now=now)
        if updated is not None:
            self._advance_slot(updated.submission_id, kind="failure_notice", actor=actor)
            return self.storage.get_email_submission(updated.submission_id)
        return None

    def retry_due_notifications(
        self,
        *,
        limit: int = 25,
        actor: str,
        counts: dict[str, int] | None = None,
    ) -> None:
        now = self.now()
        handled = 0
        for sub in self.storage.list_email_submissions():
            if handled >= limit:
                break
            if sub.next_attempt_at is not None and sub.next_attempt_at > now:
                continue
            did_work = False
            if self._slot_due(sub, kind="receipt", now=now):
                self._advance_slot(sub.submission_id, kind="receipt", actor=actor, counts=counts)
                did_work = True
                sub = self.storage.get_email_submission(sub.submission_id) or sub
            if self._slot_due(sub, kind="failure_notice", now=now):
                self._advance_slot(
                    sub.submission_id, kind="failure_notice", actor=actor, counts=counts
                )
                did_work = True
            if did_work:
                handled += 1

    def _slot_due(self, sub: EmailSubmission, *, kind: str, now: datetime) -> bool:
        status = getattr(sub, f"{kind}_status")
        lease_until = getattr(sub, f"{kind}_lease_until")
        if status in {"pending", "uncertain"}:
            return True
        if status == "sending" and (lease_until is None or lease_until <= now):
            return True
        return False

    def _advance_slot(
        self,
        submission_id: UUID,
        *,
        kind: str,
        actor: str,
        counts: dict[str, int] | None = None,
    ) -> None:
        sub = self.storage.get_email_submission(submission_id)
        if sub is None:
            return
        now = self.now()
        if not self._slot_due(sub, kind=kind, now=now):
            return

        status_f = f"{kind}_status"
        attempt_id_f = f"{kind}_attempt_id"
        lease_f = f"{kind}_lease_until"
        attempts_f = f"{kind}_attempts"
        sent_at_f = f"{kind}_sent_at"
        msg_id_f = f"{kind}_gmail_message_id"

        cur_status = getattr(sub, status_f)
        cur_attempt_id = getattr(sub, attempt_id_f)
        cur_attempts = getattr(sub, attempts_f)

        # 1. Reconcile any prior uncertain / expired-lease attempt before sending again!
        if cur_attempt_id is not None and cur_status in {"uncertain", "sending"}:
            try:
                found_id = self.gmail.check_receipt(
                    thread_id=sub.gmail_thread_id, receipt_id=cur_attempt_id
                )
            except GmailError as exc:
                backoff = now + timedelta(seconds=_backoff_seconds(max(1, cur_attempts)))
                self.storage.change_email_submission(
                    sub,
                    {
                        status_f: "uncertain",
                        "last_error": exc.code,
                        "next_attempt_at": backoff,
                    },
                    actor=actor,
                    now=now,
                )
                return

            if found_id is not None:
                self.storage.change_email_submission(
                    sub,
                    {
                        status_f: "sent",
                        sent_at_f: now,
                        msg_id_f: found_id,
                        lease_f: None,
                        "last_error": None,
                        "next_attempt_at": self._remaining_due(sub, finished_kind=kind, now=now),
                    },
                    actor=actor,
                    now=now,
                )
                if counts is not None:
                    counts["receipts_attempted"] += 1
                    counts["receipts_sent"] += 1
                return

            if cur_attempts >= self.settings.email_receipt_max_attempts:
                self.storage.change_email_submission(
                    sub,
                    {
                        status_f: "failed",
                        lease_f: None,
                        "next_attempt_at": self._remaining_due(sub, finished_kind=kind, now=now),
                    },
                    actor=actor,
                    now=now,
                )
                if counts is not None:
                    counts["receipts_attempted"] += 1
                    counts["receipts_failed"] += 1
                return

        if counts is not None:
            counts["receipts_attempted"] += 1

        # 2. Enforce durable per-member and global hourly outbound send budgets.
        allowed = self.storage.reserve_email_outbound_send(
            person_id=sub.submitter_person_id,
            now=now,
            member_hourly_limit=self.settings.email_receipt_member_hourly_limit,
            global_hourly_limit=self.settings.email_receipt_global_hourly_limit,
        )
        if not allowed:
            self.storage.change_email_submission(
                sub,
                {
                    status_f: "suppressed_rate_limit",
                    lease_f: None,
                    "next_attempt_at": self._remaining_due(sub, finished_kind=kind, now=now),
                },
                actor=actor,
                now=now,
            )
            if counts is not None:
                counts["receipts_suppressed"] += 1
            return

        # 3. Acquire send lease via CAS before touching Gmail.
        attempt_id = uuid4()
        next_attempts = cur_attempts + 1
        leased = self.storage.change_email_submission(
            sub,
            {
                status_f: "sending",
                attempt_id_f: attempt_id,
                lease_f: now + timedelta(seconds=self.settings.email_receipt_lease_s),
                attempts_f: next_attempts,
            },
            actor=actor,
            now=now,
        )
        if leased is None:
            return

        body_text = (
            _render_receipt_body(leased) if kind == "receipt" else _render_failure_body(leased)
        )
        try:
            sent_id = self.gmail.send_reply(
                thread_id=leased.gmail_thread_id,
                to_email=leased.verified_sender_email,
                subject=_reply_subject(leased.subject),
                body_text=body_text,
                in_reply_to=leased.outer_message_id,
                receipt_id=attempt_id,
            )
        except GmailError as exc:
            backoff = now + timedelta(seconds=_backoff_seconds(next_attempts))
            if exc.uncertain:
                self.storage.change_email_submission(
                    leased,
                    {
                        status_f: "uncertain",
                        lease_f: None,
                        "last_error": exc.code,
                        "next_attempt_at": backoff,
                    },
                    actor=actor,
                    now=now,
                )
                if counts is not None:
                    counts["receipts_failed"] += 1
                return
            exhausted = next_attempts >= self.settings.email_receipt_max_attempts
            self.storage.change_email_submission(
                leased,
                {
                    status_f: "failed" if exhausted else "pending",
                    attempt_id_f: None,
                    lease_f: None,
                    "last_error": exc.code,
                    "next_attempt_at": (
                        self._remaining_due(leased, finished_kind=kind, now=now)
                        if exhausted
                        else backoff
                    ),
                },
                actor=actor,
                now=now,
            )
            if counts is not None:
                counts["receipts_failed"] += 1
            return

        self.storage.change_email_submission(
            leased,
            {
                status_f: "sent",
                sent_at_f: now,
                msg_id_f: sent_id,
                lease_f: None,
                "last_error": None,
                "next_attempt_at": self._remaining_due(leased, finished_kind=kind, now=now),
            },
            actor=actor,
            now=now,
        )
        if counts is not None:
            counts["receipts_sent"] += 1

    def _remaining_due(
        self, sub: EmailSubmission, *, finished_kind: str, now: datetime
    ) -> datetime | None:
        other = "failure_notice" if finished_kind == "receipt" else "receipt"
        if self._slot_due(sub, kind=other, now=now):
            return now
        return None
