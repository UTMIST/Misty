"""The same recovery contract runs against memory and real Postgres."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from contracts.fetcher import FetchResult, SourcePermission
from contracts.meeting import MeetingPayload, TaggedDoc, WriterError
from src.config import Settings
from src.meeting_records import MeetingRecords

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
TEAM = uuid4()
PERSON = uuid4()


def payload():
    return MeetingPayload(
        guild_id="1",
        text_channel_id="2",
        voice_channel_id="3",
        requester_discord_id="4",
        started_at=NOW - timedelta(hours=1),
        ended_at=NOW,
        title="Planning <draft>",
        participants=[
            {"discord_id": "4", "display_name": "Angela"},
            {"discord_id": "5", "display_name": "Guest"},
        ],
        summary="Summary",
        decisions=["Ship"],
        action_items=["Angela: test"],
        transcript="[00:01] Angela: one\n[00:02] Guest: two <three>\n[00:03] Angela: four",
    )


class Directory:
    def get_meeting_audience(self, guild_id, channel_id):
        assert (guild_id, channel_id) == ("1", "3")
        return {TEAM: "team@example.org"}

    def get_person_id_by_discord(self, discord_id):
        return PERSON if discord_id == "4" else None

    def get_person_id_by_verified_email(self, email):
        return None

    def get_team_id_by_synced_google_group(self, email):
        return TEAM if email == "team@example.org" else None


class Writer:
    def __init__(self):
        self.docs = {}
        self.created = []
        self.shares = []
        self.deleted = []
        self.fail = None
        self.on_create = None
        self.delete_fails = False
        self.state = "exists"

    def create(self, **kwargs):
        file_id = f"doc{len(self.created)}"
        self.created.append(kwargs)
        self.docs[file_id] = TaggedDoc(
            document_id=file_id, attempt_id=kwargs["attempt_id"], created_at=NOW, trashed=False
        )
        if self.on_create:
            self.on_create(file_id)
        if self.fail == "create":
            raise WriterError("google_transport", uncertain=True)
        return file_id

    def search(self, meeting_id):
        return list(self.docs.values())

    def share(self, document_id, commenters):
        if self.fail == "share":
            raise WriterError("denied")
        if self.fail == "unsafe":
            raise WriterError("unexpected_acl")
        self.shares.append(document_id)
        return []

    def fetch(self, document_id):
        if self.fail == "fetch":
            raise WriterError("google_404")
        return FetchResult(
            title="Human edit",
            content="The current Google body",
            permissions=[
                SourcePermission(
                    permission_id="p",
                    principal_type="group",
                    role="commenter",
                    principal="team@example.org",
                )
            ],
        )

    def status(self, document_id):
        return self.state

    def delete(self, document_id):
        if self.delete_fails:
            raise WriterError("cleanup_failed")
        self.deleted.append(document_id)
        self.docs.pop(document_id, None)


class MeetingRecordCases:
    @pytest.fixture
    def flow(self, adapter):
        clock = [NOW]
        writer = Writer()
        records = MeetingRecords(
            adapter, writer, Directory(), Settings(_env_file=None), now=lambda: clock[0]
        )
        meeting_id = uuid4()
        r, created = records.accept(meeting_id, payload(), actor="meeting-key")
        assert created
        return records, writer, clock, r

    def test_success_survives_new_coordinator_and_replay(self, flow):
        records, writer, clock, original = flow
        records.advance(original.meeting_id)
        r = records.storage.get_meeting(original.meeting_id)
        assert r.persistence_status == "saved"
        assert r.pending_payload is None
        assert r.requester_person_id == PERSON
        assert [p["person_id"] for p in r.participants] == [str(PERSON), None]
        assert r.decisions == ["Ship"] and r.action_items == ["Angela: test"]
        html = writer.created[0]["html"]
        assert "&lt;draft&gt;" in html and "&lt;three&gt;" in html
        assert html.index("[00:01]") < html.index("[00:02]") < html.index("[00:03]")
        for section in ["Minutes", "Decisions", "Action items", "Full transcript"]:
            assert f"<h2>{section}</h2>" in html
        doc = records.storage.get_doc(r.doc_id)
        assert doc.owning_person_id == PERSON and doc.owning_team_id == TEAM
        assert records.storage.list_grants(doc.id)[0].grantee_id == TEAM
        assert records.storage.get_doc_content(doc.id) == "The current Google body"
        restarted = MeetingRecords(
            records.storage, writer, Directory(), records.settings, now=lambda: clock[0]
        )
        replay, created = restarted.accept(r.meeting_id, payload(), actor="another-key")
        assert not created and replay.doc_id == r.doc_id
        restarted.advance(r.meeting_id)
        assert len(writer.created) == 1
        with pytest.raises(ValueError, match="conflict"):
            restarted.accept(
                r.meeting_id,
                payload().model_copy(update={"transcript": "different"}),
                actor="another-key",
            )

    @pytest.mark.parametrize("failure", ["share", "fetch"])
    def test_partial_failure_resumes_canonical_without_rewrite(self, flow, failure):
        records, writer, clock, original = flow
        writer.fail = failure
        records.advance(original.meeting_id)
        r = records.storage.get_meeting(original.meeting_id)
        assert r.google_document_id and r.pending_payload is None
        assert r.persistence_status != "saved"
        writer.fail = None
        clock[0] += timedelta(hours=1)
        records.retry_due(10)
        saved = records.storage.get_meeting(r.meeting_id)
        assert (
            saved.persistence_status == "saved" and saved.google_document_id == r.google_document_id
        )
        assert len(writer.created) == 1

    def test_uncertain_create_is_found_after_lease_without_duplicate(self, flow):
        records, writer, clock, original = flow
        writer.fail = "create"
        records.advance(original.meeting_id)
        r = records.storage.get_meeting(original.meeting_id)
        assert r.persistence_status == "create_uncertain" and r.pending_payload
        records.advance(r.meeting_id)
        assert len(writer.created) == 1
        writer.fail = None
        clock[0] += timedelta(minutes=11)
        records.advance(r.meeting_id)
        assert records.storage.get_meeting(r.meeting_id).persistence_status == "saved"
        assert len(writer.created) == 1

    def test_late_creation_cannot_replace_saved_canonical(self, flow):
        records, writer, clock, original = flow

        def delay(file_id):
            # The first request is still running after a replacement acquires
            # the lease, creates another Doc and reaches saved.
            late = writer.docs.pop(file_id)
            writer.on_create = None
            clock[0] += timedelta(minutes=11)
            records.advance(original.meeting_id)
            writer.docs[file_id] = late.model_copy(update={"created_at": NOW - timedelta(days=1)})

        writer.on_create = delay
        records.advance(original.meeting_id)
        saved = records.storage.get_meeting(original.meeting_id)
        assert saved.persistence_status == "saved"
        assert saved.google_document_id != "doc0" and "doc0" not in writer.shares
        clock[0] += timedelta(hours=2)
        records.retry_due(10)
        assert writer.deleted == ["doc0"]
        assert (
            records.storage.get_meeting(saved.meeting_id).google_document_id
            == saved.google_document_id
        )
        assert len(writer.created) == 2

    @pytest.mark.parametrize("terminal", ["failed", "removed"])
    def test_recurring_empty_scans_then_late_file_and_cleanup_retry(self, flow, terminal):
        records, writer, clock, original = flow
        r = records.storage.change_meeting(
            original,
            {
                "persistence_status": "failed",
                "next_reconcile_at": NOW,
                "last_error": "attempt_limit",
            },
            actor="test",
        )
        if terminal == "removed":
            records.remove(r.meeting_id, actor="admin")
        for _ in range(3):
            clock[0] += timedelta(hours=2)
            records.retry_due(10)
        writer.docs["late"] = TaggedDoc(
            document_id="late", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        writer.delete_fails = True
        clock[0] += timedelta(hours=2)
        records.retry_due(10)
        assert "late" in writer.docs
        writer.delete_fails = False
        clock[0] += timedelta(hours=2)
        records.retry_due(10)
        assert "late" not in writer.docs
        assert not writer.created and not writer.shares
        assert records.storage.get_meeting(r.meeting_id).persistence_status == terminal

    def test_removal_during_create_rejects_late_completion(self, flow):
        records, writer, clock, original = flow
        writer.on_create = lambda _: records.remove(original.meeting_id, actor="admin")
        records.advance(original.meeting_id)
        r = records.storage.get_meeting(original.meeting_id)
        assert r.removed_at and not r.google_document_id
        assert not writer.shares and not r.pending_payload
        assert records.storage.list_docs() == []

    def test_unexpected_acl_is_terminal_security_removal(self, flow):
        records, writer, clock, original = flow
        writer.fail = "unsafe"
        records.advance(original.meeting_id)
        r = records.storage.get_meeting(original.meeting_id)
        assert r.persistence_status == "removed" and r.removed_by == "security:unexpected_acl"
        assert r.delivery_status == "disabled" and r.last_error == "unexpected_acl"
        assert not r.participants and not r.decisions and r.summary is None
        records.advance(r.meeting_id, explicit=True)
        assert len(writer.created) == 1

    def test_stale_cas_and_catalog_cannot_undo_removal(self, flow):
        records, writer, clock, original = flow
        records.advance(original.meeting_id)
        saved = records.storage.get_meeting(original.meeting_id)
        removed = records.remove(saved.meeting_id, actor="admin")
        assert removed.persistence_status == "removed"
        assert (
            records.storage.change_meeting(saved, {"persistence_status": "saved"}, actor="late")
            is None
        )
        assert not records.storage.catalog_meeting(
            saved, writer.fetch("x"), [], expires_at=NOW + timedelta(days=1), actor="late"
        )
        doc = records.storage.get_doc(saved.doc_id)
        assert doc.purged_at and not doc.active and not doc.content_snapshot
        assert records.storage.get_doc_content(doc.id) is None
        assert not records.storage.list_grants(doc.id)
        records.storage.upsert_doc_content(
            doc.id, content_text="late", content_hash="h", fetched_at=NOW
        )
        assert records.storage.get_doc_content(doc.id) is None
        assert records.storage.update_doc(doc.id, {"active": True}, actor="late") is None

    def test_catalog_failure_keeps_identity_and_retries_without_duplicate(
        self, flow, monkeypatch, caplog
    ):
        records, writer, clock, original = flow
        catalog = records.storage.catalog_meeting

        def fail(*args, **kwargs):
            raise RuntimeError("PRIVATE SQL PARAMETERS")

        monkeypatch.setattr(records.storage, "catalog_meeting", fail)
        records.advance(original.meeting_id)
        r = records.storage.get_meeting(original.meeting_id)
        assert r.google_document_id and r.pending_payload is None and r.doc_id is None
        assert r.persistence_status == "shared"
        assert "PRIVATE SQL PARAMETERS" not in caplog.text
        monkeypatch.setattr(records.storage, "catalog_meeting", catalog)
        clock[0] += timedelta(hours=1)
        records.advance(r.meeting_id)
        assert records.storage.get_meeting(r.meeting_id).persistence_status == "saved"
        assert len(writer.created) == 1
