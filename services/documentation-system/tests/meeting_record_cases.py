"""The same recovery contract runs against memory and real Postgres."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from contracts.fetcher import FetchResult, SourcePermission
from contracts.meeting import MeetingPayload, TaggedDoc, WriterError
from src.config import Settings
from src.meeting_records import MeetingRecords
from tests.fake_embedder import FakeEmbedder, fake_vector
from contracts.retrieval import QueryEmbedding
from contracts.visibility import Actor

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
    def get_team_label(self, team_id):
        return "Team" if team_id == TEAM else None

    def get_person_label(self, person_id):
        return "Requester" if person_id == PERSON else None

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
    def __init__(self, *, prefix=""):
        self.prefix = prefix
        self.docs = {}
        self.created = []
        self.shares = []
        self.deleted = []
        self.fail = None
        self.on_create = None
        self.delete_fails = False
        self.state = "exists"

    def create(self, **kwargs):
        file_id = f"{self.prefix}doc{len(self.created)}"
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
        if document_id not in self.docs:
            raise WriterError("delete_unconfirmed")
        self.deleted.append(document_id)
        self.docs.pop(document_id, None)


class MeetingRecordCases:
    @pytest.fixture
    def flow(self, adapter):
        clock = [NOW]
        writer = Writer()
        records = MeetingRecords(
            adapter,
            writer,
            Directory(),
            Settings(_env_file=None),
            embedder=FakeEmbedder(),
            now=lambda: clock[0],
        )
        meeting_id = uuid4()
        r, created = records.accept(meeting_id, payload(), actor="meeting-key")
        assert created
        return records, writer, clock, r

    @pytest.fixture
    def meeting_api(self, flow):
        from src.api.app import create_app
        from src.api.deps import get_directory, get_fetchers, get_storage
        from src.api.routers.meeting_records import get_records
        from src.google_identity import google_file_id

        records, writer, clock, original = flow
        app = create_app()
        app.dependency_overrides[get_storage] = lambda: records.storage
        app.dependency_overrides[get_directory] = lambda: records.directory
        app.dependency_overrides[get_records] = lambda: records
        app.dependency_overrides[get_fetchers] = lambda: SimpleNamespace(
            meeting_writer=writer, fetch_for=lambda source, url: writer.fetch(google_file_id(url))
        )
        with TestClient(app) as client:
            yield client, {"X-API-Key": "dev-api-key-change-me"}

    def test_partial_persistence_ordinary_ingest_and_removal_purge_all_identities(
        self, flow, meeting_api
    ):
        records, writer, clock, original = flow
        client, headers = meeting_api
        writer.fail = "fetch"
        records.advance(original.meeting_id)
        current = records.storage.get_meeting(original.meeting_id)
        assert current.google_document_id and current.doc_id is None
        writer.fail = None
        duplicate_id = "duplicate"
        writer.docs[duplicate_id] = TaggedDoc(
            document_id=duplicate_id, attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        urls = [current.doc_url, f"https://docs.google.com/document/d/{duplicate_id}/edit"]
        ids = []
        for url in urls:
            response = client.post("/docs", headers=headers, json={"url": url})
            assert response.status_code == 201, response.text
            doc_id = UUID(response.json()["doc"]["id"])
            ids.append(doc_id)
            assert records.storage.get_doc_content(doc_id)
            assert records.storage.list_grants(doc_id)
            assert records.storage.list_doc_chunks(doc_id)
        # Simulate a pre-dedup catalog alias, which a first-match lookup misses.
        from src.indexing import index_doc
        from src.content import content_hash

        alias = f"https://docs.google.com/document/u/0/d/{current.google_document_id}/edit"
        legacy = records.storage.create_doc(
            url=alias,
            url_normalized=alias,
            source_id="gdocs",
            title="Legacy alias",
            description=None,
            owning_team_id=None,
            owning_team_label=None,
            owning_person_id=None,
            owning_person_label=None,
            content_snapshot="Google body",
            fetched_at=NOW,
            tags=["legacy"],
            actor="legacy",
        )
        records.storage.upsert_doc_content(
            legacy.id,
            content_text="Google body",
            content_hash=content_hash("Google body"),
            fetched_at=NOW,
        )
        records.storage.add_grant(legacy.id, grantee_type="team", grantee_id=TEAM, actor="legacy")
        assert (
            index_doc(legacy.id, storage=records.storage, embedder=records.embedder).status
            == "indexed"
        )
        ids.append(legacy.id)
        urls.append(alias)
        writer.docs["unregistered"] = TaggedDoc(
            document_id="unregistered", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        assert records.storage.get_meeting(original.meeting_id).doc_id is None
        response = client.post(f"/meeting-records/{original.meeting_id}/remove", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["persistence_status"] == "removed"
        assert not writer.docs
        assert (
            client.post(
                "/docs",
                headers=headers,
                json={"url": "https://docs.google.com/document/d/unregistered/edit"},
            ).status_code
            == 400
        )
        for doc_id, url in zip(ids, urls):
            doc = records.storage.get_doc(doc_id)
            assert doc.purged_at and not doc.active and doc.content_snapshot is None
            assert not records.storage.get_doc_content(doc_id)
            assert not records.storage.list_grants(doc_id)
            assert not records.storage.list_doc_chunks(doc_id)
            assert client.post("/docs", headers=headers, json={"url": url}).status_code == 400
        assert not records.storage.search_chunks(
            query="Google body",
            embedding=QueryEmbedding(
                model=records.embedder.model, vector=fake_vector("Google body")
            ),
            visibility=Actor(PERSON, frozenset(), frozenset({TEAM})),
            k=10,
            min_cosine=0.1,
        )

    @pytest.mark.parametrize("insert_first", [False, True])
    def test_ingest_cannot_escape_removal_during_registration(
        self, flow, meeting_api, monkeypatch, insert_first
    ):
        records, writer, clock, original = flow
        client, headers = meeting_api
        writer.fail = "fetch"
        records.advance(original.meeting_id)
        writer.fail = None
        current = records.storage.get_meeting(original.meeting_id)
        create = records.storage.create_doc

        def interleaved_create(**kwargs):
            doc = create(**kwargs) if insert_first else None
            response = client.post(
                f"/meeting-records/{original.meeting_id}/remove", headers=headers
            )
            assert response.status_code == 200 and response.json()["removed_at"]
            return doc if insert_first else create(**kwargs)

        monkeypatch.setattr(records.storage, "create_doc", interleaved_create)
        response = client.post("/docs", headers=headers, json={"url": current.doc_url})
        assert response.status_code == (201 if insert_first else 400), response.text
        for doc in records.storage.list_docs(active_only=False):
            assert doc.purged_at and not records.storage.get_doc_content(doc.id)
            assert not records.storage.list_grants(doc.id) and not records.storage.list_doc_chunks(
                doc.id
            )

    def test_reconciliation_purges_an_ingested_late_duplicate(self, flow, meeting_api):
        records, writer, clock, original = flow
        client, headers = meeting_api
        records.advance(original.meeting_id)
        records.remove(original.meeting_id, actor="admin")
        writer.docs["late"] = TaggedDoc(
            document_id="late", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        url = "https://docs.google.com/document/d/late/edit"
        response = client.post("/docs", headers=headers, json={"url": url})
        assert response.status_code == 201
        doc_id = UUID(response.json()["doc"]["id"])
        assert records.storage.list_doc_chunks(doc_id)
        clock[0] += timedelta(hours=2)
        records.reconcile(original.meeting_id)
        assert records.storage.get_doc(doc_id).purged_at
        assert not records.storage.get_doc_content(doc_id)
        assert not records.storage.list_grants(doc_id)
        assert not records.storage.list_doc_chunks(doc_id)
        assert not writer.docs
        assert records.storage.get_meeting_by_google_id("late").removed_at
        assert client.post("/docs", headers=headers, json={"url": url}).status_code == 400

    def test_old_removed_record_is_purged_even_when_google_search_is_empty(self, flow, meeting_api):
        records, writer, clock, original = flow
        client, headers = meeting_api
        writer.fail = "fetch"
        records.advance(original.meeting_id)
        writer.fail = None
        current = records.storage.get_meeting(original.meeting_id)
        response = client.post("/docs", headers=headers, json={"url": current.doc_url})
        doc_id = UUID(response.json()["doc"]["id"])
        # Reproduce a tombstone left by the previous removal implementation.
        records.storage.change_meeting(
            current,
            {
                "removed_at": NOW,
                "persistence_status": "removed",
                "next_reconcile_at": NOW,
                "google_deleted_at": NOW,
            },
            actor="old-removal",
        )
        writer.docs.clear()
        records.reconcile(original.meeting_id)
        assert records.storage.get_doc(doc_id).purged_at
        assert not records.storage.get_doc_content(doc_id)
        assert not records.storage.list_grants(doc_id)
        assert not records.storage.list_doc_chunks(doc_id)

    def test_saved_content_is_immediately_searchable_for_the_source_audience(self, flow):
        records, writer, clock, original = flow
        records.advance(original.meeting_id)
        r = records.storage.get_meeting(original.meeting_id)
        assert r.persistence_status == "saved" and not r.index_pending
        assert records.storage.list_doc_chunks(r.doc_id)
        embedding = QueryEmbedding(model=records.embedder.model, vector=fake_vector("Google body"))
        hits = records.storage.search_chunks(
            query="Google body",
            embedding=embedding,
            visibility=Actor(PERSON, frozenset(), frozenset({TEAM})),
            k=10,
            min_cosine=0.1,
        )
        assert {hit.doc_id for hit in hits} == {r.doc_id}
        assert not records.storage.search_chunks(
            query="Google body",
            embedding=embedding,
            visibility=Actor(uuid4(), frozenset()),
            k=10,
            min_cosine=0.1,
        )

    def test_catalog_purge_requires_marker_and_preserves_identity_fences(self, flow):
        records, writer, clock, original = flow
        with pytest.raises(ValueError, match="removal marker"):
            records.storage.purge_meeting_catalog(original.meeting_id, ["duplicate"], actor="admin")
        records.advance(original.meeting_id)
        removed = records.remove(original.meeting_id, actor="admin")
        assert removed.removed_google_document_ids == [removed.google_document_id]
        with pytest.raises(ValueError, match="cannot be forgotten"):
            records.storage.change_meeting(
                removed, {"removed_google_document_ids": []}, actor="late"
            )

    def test_failed_initial_index_retries_after_restart_without_google_writes(self, flow):
        records, writer, clock, original = flow
        records.embedder.fail_on_call = 1
        records.advance(original.meeting_id)
        r = records.storage.get_meeting(original.meeting_id)
        assert r.persistence_status == "saved" and r.index_pending
        assert r.last_error == "index_pending"
        assert not records.storage.list_doc_chunks(r.doc_id)
        restarted = MeetingRecords(
            records.storage,
            writer,
            Directory(),
            records.settings,
            embedder=FakeEmbedder(),
            now=lambda: clock[0],
        )
        clock[0] += timedelta(minutes=6)
        assert restarted.retry_due(10) == 1
        r = records.storage.get_meeting(r.meeting_id)
        assert not r.index_pending and r.last_error is None
        assert records.storage.list_doc_chunks(r.doc_id)
        assert len(writer.created) == len(writer.shares) == 1
        restarted.advance(r.meeting_id)
        assert len(restarted.embedder.calls) == 1

    def test_removal_resumes_after_canonical_delete_and_duplicate_failure(self, flow, monkeypatch):
        records, writer, clock, original = flow
        records.advance(original.meeting_id)
        r = records.storage.get_meeting(original.meeting_id)
        writer.docs["duplicate"] = TaggedDoc(
            document_id="duplicate", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        delete = writer.delete

        def fail_duplicate(file_id):
            if file_id == "duplicate":
                raise WriterError("cleanup_failed")
            delete(file_id)

        monkeypatch.setattr(writer, "delete", fail_duplicate)
        removing = records.remove(r.meeting_id, actor="admin")
        assert removing.persistence_status == "removing" and removing.google_deleted_at == NOW
        assert r.google_document_id not in writer.docs
        monkeypatch.setattr(writer, "delete", delete)
        clock[0] += timedelta(hours=2)
        records.retry_due(10)
        removed = records.storage.get_meeting(r.meeting_id)
        assert removed.persistence_status == "removed" and not writer.docs
        assert writer.deleted.count(r.google_document_id) == 1
        with pytest.raises(ValueError, match="deletion is immutable"):
            records.storage.change_meeting(removed, {"google_deleted_at": None}, actor="late")

    def test_removal_receipt_survives_final_database_write_failure(self, flow, monkeypatch):
        records, writer, clock, original = flow
        records.advance(original.meeting_id)
        change = records.storage.change_meeting

        def fail_final(record, values, **kwargs):
            if values.get("persistence_status") == "removed":
                raise RuntimeError("database temporarily unavailable")
            return change(record, values, **kwargs)

        monkeypatch.setattr(records.storage, "change_meeting", fail_final)
        with pytest.raises(RuntimeError):
            records.remove(original.meeting_id, actor="admin")
        assert records.storage.get_meeting(original.meeting_id).google_deleted_at == NOW
        monkeypatch.setattr(records.storage, "change_meeting", change)
        clock[0] += timedelta(hours=2)
        records.retry_due(10)
        assert records.storage.get_meeting(original.meeting_id).persistence_status == "removed"
        assert len(writer.deleted) == 1

    def test_unknown_404_is_not_a_confirmed_deletion(self, flow):
        records, writer, clock, original = flow
        records.advance(original.meeting_id)
        writer.docs.clear()  # Lost owner access is indistinguishable from deletion.
        r = records.remove(original.meeting_id, actor="admin")
        assert r.persistence_status == "removing" and r.google_deleted_at is None

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
