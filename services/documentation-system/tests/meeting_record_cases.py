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
    def get_active_team_ids(self, person_id):
        return frozenset({TEAM})

    def get_source_access_team_ids(self, person_id):
        return frozenset({TEAM})

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
        from src.api.deps import get_directory, get_embeddings, get_fetchers, get_storage
        from src.api.routers.meeting_records import get_records
        from src.google_identity import google_file_id

        records, writer, clock, original = flow
        app = create_app()
        app.dependency_overrides[get_storage] = lambda: records.storage
        app.dependency_overrides[get_directory] = lambda: records.directory
        app.dependency_overrides[get_records] = lambda: records
        app.dependency_overrides[get_embeddings] = lambda: SimpleNamespace(
            embed_query=lambda query: QueryEmbedding(
                model=records.embedder.model, vector=fake_vector(query)
            )
        )
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

    @pytest.mark.parametrize("deletion_state", ["pending", "confirmed"])
    def test_removal_purges_previously_reconciled_duplicate_identities(
        self, flow, meeting_api, deletion_state
    ):
        from src.api.hashing import generate_key

        records, writer, clock, original = flow
        client, headers = meeting_api
        records.advance(original.meeting_id)
        writer.docs["duplicate"] = TaggedDoc(
            document_id="duplicate", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        url = "https://docs.google.com/document/d/duplicate/edit"
        response = client.post("/docs", headers=headers, json={"url": url})
        assert response.status_code == 201, response.text
        doc_id = UUID(response.json()["doc"]["id"])
        token, prefix, key_hash = generate_key()
        records.storage.create_api_key(
            name="meeting-reader",
            prefix=prefix,
            key_hash=key_hash,
            scopes=["docs:read", "act-as-user"],
            actor="test",
        )
        reader = {"X-API-Key": token, "X-On-Behalf-Of": str(PERSON)}
        response = client.post("/search", headers=reader, json={"query": "Google body"})
        assert response.status_code == 200, response.text
        assert str(doc_id) in {hit["doc_id"] for hit in response.json()["results"]}
        writer.delete_fails = deletion_state == "pending"
        clock[0] += timedelta(hours=1)
        records.reconcile(original.meeting_id)
        assert (
            records.storage.get_meeting(original.meeting_id).google_deletions["duplicate"]
            == deletion_state
        )
        # A pending delete may complete without acknowledgement; neither state
        # is guaranteed to remain visible to a later owner search.
        writer.docs.pop("duplicate", None)
        writer.delete_fails = False
        response = client.post(f"/meeting-records/{original.meeting_id}/remove", headers=headers)
        assert response.status_code == 200, response.text
        removed = records.storage.get_meeting(original.meeting_id)
        assert "duplicate" in removed.removed_google_document_ids
        doc = records.storage.get_doc(doc_id)
        assert doc.purged_at and not doc.active and doc.content_snapshot is None
        assert not records.storage.get_doc_content(doc_id)
        assert not records.storage.list_grants(doc_id)
        assert not records.storage.list_doc_chunks(doc_id)
        response = client.post("/search", headers=reader, json={"query": "Google body"})
        assert response.status_code == 200 and response.json()["results"] == []
        assert client.post("/docs", headers=headers, json={"url": url}).status_code == 400
        # Google URL aliases must observe the same permanent identity fence.
        alias = "https://docs.google.com/document/u/0/d/duplicate/edit"
        assert client.post("/docs", headers=headers, json={"url": alias}).status_code == 400

    @pytest.mark.parametrize("previously_removed", [False, True])
    @pytest.mark.parametrize("delete_error", ["google_transport", "delete_unconfirmed"])
    def test_unlisted_pending_duplicate_keeps_removal_and_monitoring_incomplete(
        self, flow, meeting_api, monkeypatch, capsys, delete_error, previously_removed
    ):
        from pydantic import SecretStr
        from src import source_refresh
        from src.source_refresh import DocsApiClient, RunMonitor

        records, writer, clock, original = flow
        client, headers = meeting_api
        records.advance(original.meeting_id)
        writer.docs["duplicate"] = TaggedDoc(
            document_id="duplicate", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        writer.docs["unattempted"] = TaggedDoc(
            document_id="unattempted", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        delete = writer.delete
        blocked = [True]
        attempts = []

        def delayed_delete(document_id):
            attempts.append(document_id)
            if document_id == "duplicate" and blocked[0]:
                raise WriterError(delete_error, uncertain=True)
            return delete(document_id)

        monkeypatch.setattr(writer, "delete", delayed_delete)
        response = client.post(f"/meeting-records/{original.meeting_id}/remove", headers=headers)
        assert response.status_code == 200
        assert response.json()["persistence_status"] == "removing"
        assert response.json()["google_deletions"]["duplicate"] == "pending"
        assert "unattempted" in response.json()["removed_google_document_ids"]
        assert "unattempted" not in response.json()["google_deletions"]
        # The provider still holds the file, but listing loses visibility.
        monkeypatch.setattr(writer, "search", lambda meeting_id: [])
        if previously_removed:
            # Repair tombstones marked complete by the previous implementation.
            records.storage.change_meeting(
                records.storage.get_meeting(original.meeting_id),
                {"persistence_status": "removed"},
                actor="old-removal",
            )
        clock[0] += timedelta(hours=1)
        response = client.post("/meeting-records/retry-due", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "attempted": 1,
            "succeeded": 0,
            "failed": 1,
            "persistence_failed": 0,
            "index_failed": 0,
            "cleanup_failed": 1,
        }
        assert attempts.count("duplicate") == 2 and "duplicate" in writer.docs
        events = []
        monkeypatch.setenv("DOCS_API_KEY", headers["X-API-Key"])
        monkeypatch.setenv("SOURCE_REFRESH_MONITOR_URL", "https://monitor.test/test-run")
        monkeypatch.setattr(RunMonitor, "ping", lambda self, event: events.append(event) or True)
        monkeypatch.setattr(
            source_refresh,
            "DocsApiClient",
            lambda **kwargs: DocsApiClient(
                base_url="http://testserver", api_key=SecretStr(headers["X-API-Key"]), client=client
            ),
        )
        clock[0] += timedelta(hours=1)
        assert source_refresh.main(["--meeting-records"]) == 1
        assert events == ["start", "fail"]
        assert "meeting_cleanup_failed=1" in capsys.readouterr().out
        current = records.storage.get_meeting(original.meeting_id)
        assert current.persistence_status == "removing"
        assert current.google_deletions["duplicate"] == "pending"
        # A real acknowledgement, independent of listing, permits completion.
        blocked[0] = False
        events.clear()
        clock[0] += timedelta(hours=1)
        assert source_refresh.main(["--meeting-records"]) == 0
        assert events == ["start", "success"]
        current = records.storage.get_meeting(original.meeting_id)
        assert current.persistence_status == "removed"
        assert current.google_deletions["duplicate"] == "confirmed"
        assert attempts.count("doc0") == 1 and not writer.docs

    def test_reconciliation_retries_unlisted_pending_duplicate(self, flow, monkeypatch):
        records, writer, clock, original = flow
        records.advance(original.meeting_id)
        writer.docs["duplicate"] = TaggedDoc(
            document_id="duplicate", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        writer.delete_fails = True
        clock[0] += timedelta(hours=1)
        records.reconcile(original.meeting_id)
        assert records.storage.get_meeting(original.meeting_id).google_deletions == {
            "duplicate": "pending"
        }
        monkeypatch.setattr(writer, "search", lambda meeting_id: [])
        clock[0] += timedelta(hours=1)
        result = records.retry_due(10)
        assert result.failed == result.cleanup_failed == 1
        writer.delete_fails = False
        clock[0] += timedelta(hours=1)
        result = records.retry_due(10)
        assert result.succeeded == 1 and not result.failed
        current = records.storage.get_meeting(original.meeting_id)
        assert current.persistence_status == "saved" and not current.last_error
        assert current.google_deletions["duplicate"] == "confirmed"
        assert set(writer.docs) == {current.google_document_id}

    def test_removal_retries_marker_when_background_finalization_wins_cas(
        self, flow, meeting_api, monkeypatch
    ):
        records, writer, clock, original = flow
        client, headers = meeting_api
        change = records.storage.change_meeting
        raced = []

        def interleaved_change(record, values, **kwargs):
            if "removed_at" in values and not raced:
                raced.append(True)
                records.advance(original.meeting_id)
                assert (
                    records.storage.get_meeting(original.meeting_id).persistence_status == "saved"
                )
            return change(record, values, **kwargs)

        monkeypatch.setattr(records.storage, "change_meeting", interleaved_change)
        response = client.post(f"/meeting-records/{original.meeting_id}/remove", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["persistence_status"] == "removed" and response.json()["removed_at"]
        assert not writer.docs
        assert all(doc.purged_at for doc in records.storage.list_docs(active_only=False))

    @pytest.mark.parametrize("failure", ["persistence", "index", "cleanup"])
    def test_real_retry_api_propagates_meeting_failures_to_runner_and_heartbeat(
        self, flow, meeting_api, monkeypatch, capsys, failure
    ):
        from pydantic import SecretStr
        from src import source_refresh
        from src.source_refresh import DocsApiClient, RunMonitor

        records, writer, clock, original = flow
        client, headers = meeting_api
        if failure == "persistence":
            records.settings.meeting_record_max_attempts = 1
            writer.fail = "share"
        elif failure == "index":
            records.embedder.fail_on_call = 1
        else:
            records.advance(original.meeting_id)
            writer.delete_fails = True
            removing = records.remove(original.meeting_id, actor="admin")
            records.storage.change_meeting(removing, {"next_reconcile_at": NOW}, actor="test")
        events = []
        monkeypatch.setenv("DOCS_API_KEY", headers["X-API-Key"])
        monkeypatch.setenv("SOURCE_REFRESH_MONITOR_URL", "https://monitor.test/test-run")
        monkeypatch.setattr(RunMonitor, "ping", lambda self, event: events.append(event) or True)
        monkeypatch.setattr(
            source_refresh,
            "DocsApiClient",
            lambda **kwargs: DocsApiClient(
                base_url="http://testserver", api_key=SecretStr(headers["X-API-Key"]), client=client
            ),
        )
        assert source_refresh.main(["--meeting-records"]) == 1
        assert events == ["start", "fail"]
        output = capsys.readouterr().out
        assert "meeting_attempted=1 meeting_failed=1" in output
        assert f"meeting_{failure}_failed=1" in output
        current = records.storage.get_meeting(original.meeting_id)
        if failure == "persistence":
            assert current.persistence_status == "failed" and current.doc_id is None
        # Known work failures release the guard after the failure heartbeat.
        owner = uuid4()
        assert records.storage.acquire_source_refresh_run(owner, actor="next-run")
        assert records.storage.release_source_refresh_run(owner)

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

    def test_retry_outcomes_report_saved_and_empty_queues(self, flow, meeting_api):
        client, headers = meeting_api
        response = client.post("/meeting-records/retry-due", headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "attempted": 1,
            "succeeded": 1,
            "failed": 0,
            "persistence_failed": 0,
            "index_failed": 0,
            "cleanup_failed": 0,
        }
        assert client.post("/meeting-records/retry-due", headers=headers).json() == {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "persistence_failed": 0,
            "index_failed": 0,
            "cleanup_failed": 0,
        }

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
        assert restarted.retry_due(10).attempted == 1
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

    def test_expired_cleanup_cannot_fence_a_new_canonical(self, flow):
        records, writer, clock, original = flow
        attempt_id = uuid4()
        records.storage.change_meeting(
            original,
            {
                "persistence_status": "failed",
                "create_attempt_id": attempt_id,
                "create_lease_until": NOW + timedelta(minutes=10),
            },
            actor="test",
        )
        writer.docs["late"] = TaggedDoc(
            document_id="late", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        clock[0] += timedelta(minutes=11)
        records.advance(original.meeting_id, explicit=True)
        saved = records.storage.get_meeting(original.meeting_id)
        assert saved.google_document_id == "late"
        records._delete(original.meeting_id, "late", cleanup_attempt=attempt_id)
        assert "late" in writer.docs
        assert not records.storage.get_meeting(original.meeting_id).google_deletions

    def test_confirmed_duplicate_deletion_is_idempotent_and_immutable(self, flow):
        records, writer, clock, original = flow
        writer.docs["duplicate"] = TaggedDoc(
            document_id="duplicate", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        records._delete(original.meeting_id, "duplicate")
        records._delete(original.meeting_id, "duplicate")
        assert writer.deleted == ["duplicate"]
        current = records.storage.get_meeting(original.meeting_id)
        assert current.google_deletions == {"duplicate": "confirmed"}
        with pytest.raises(ValueError, match="deletion progress"):
            records.storage.change_meeting(
                current, {"google_deletions": {"duplicate": "pending"}}, actor="test"
            )

    def test_uncertain_late_deletion_cannot_delete_retried_canonical(self, flow, monkeypatch):
        records, writer, clock, original = flow
        records.storage.change_meeting(
            original,
            {"persistence_status": "failed", "next_reconcile_at": NOW},
            actor="test",
        )
        writer.docs["late"] = TaggedDoc(
            document_id="late", attempt_id=uuid4(), created_at=NOW, trashed=False
        )
        delete = writer.delete

        def timeout(document_id):
            intent = records.storage.get_meeting(original.meeting_id)
            assert intent.google_deletions[document_id] == "pending"
            raise WriterError("google_transport", uncertain=True)

        monkeypatch.setattr(writer, "delete", timeout)
        records.reconcile(original.meeting_id)
        # Restart after the failed deletion releases its lease. Google still
        # lists the old file, whose delayed deletion has not completed yet.
        restarted = MeetingRecords(
            records.storage,
            writer,
            records.directory,
            records.settings,
            embedder=FakeEmbedder(),
            now=lambda: clock[0],
        )
        restarted.advance(original.meeting_id, explicit=True)
        saved = records.storage.get_meeting(original.meeting_id)
        assert saved.persistence_status == "saved"
        assert saved.google_document_id != "late"
        assert "late" not in writer.shares
        delete("late")  # The timed-out Google request finally completes.
        assert saved.google_document_id in writer.docs
        assert saved.pending_payload is None
        with pytest.raises(ValueError, match="deletion progress"):
            records.storage.change_meeting(saved, {"google_deletions": {}}, actor="test")

    @pytest.mark.parametrize("saved", [True, False])
    def test_reconciliation_clears_only_its_own_error_after_recovery(
        self, flow, monkeypatch, saved
    ):
        records, writer, clock, original = flow
        if saved:
            records.advance(original.meeting_id)
        else:
            records.storage.change_meeting(
                original,
                {
                    "persistence_status": "failed",
                    "last_error": "attempt_limit",
                    "next_reconcile_at": NOW,
                },
                actor="test",
            )
        search = writer.search

        def unavailable(meeting_id):
            raise WriterError("google_transport")

        clock[0] += timedelta(hours=2)
        monkeypatch.setattr(writer, "search", unavailable)
        assert records.retry_due(10).cleanup_failed == 1
        monkeypatch.setattr(writer, "search", search)
        clock[0] += timedelta(hours=2)
        result = records.retry_due(10)
        assert result.cleanup_failed == 0
        current = records.storage.get_meeting(original.meeting_id)
        assert current.last_error != "reconcile_failed"
        if saved:
            assert result.failed == 0 and result.succeeded == 1
        # A successful scan must not clear a persistence failure.
        records.storage.change_meeting(current, {"last_error": "denied"}, actor="test")
        clock[0] += timedelta(hours=2)
        records.reconcile(original.meeting_id)
        assert records.storage.get_meeting(original.meeting_id).last_error == "denied"

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
