from datetime import timedelta
from uuid import uuid4
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.deps import get_directory, get_fetchers, get_storage
from src.api.hashing import generate_key
from src.api.routers.meeting_records import get_records
from src.config import Settings
from src.content import content_hash
from src.meeting_records import MeetingRecords
from src.storage.in_memory import InMemoryStorageAdapter
from tests.conftest import build_seed_sources
from tests.meeting_record_cases import Directory, Writer, NOW, payload
from tests.chunk_storage_cases import make_chunk

AUTH = {"X-API-Key": "dev-api-key-change-me"}


@pytest.fixture
def flow():
    storage = InMemoryStorageAdapter(build_seed_sources())
    writer = Writer()
    directory = Directory()
    records = MeetingRecords(storage, writer, directory, Settings(_env_file=None), now=lambda: NOW)
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_records] = lambda: records
    app.dependency_overrides[get_directory] = lambda: directory
    app.dependency_overrides[get_fetchers] = lambda: SimpleNamespace(meeting_writer=writer)
    with TestClient(app) as client:
        yield client, records, writer


def test_accept_replay_conflict_status_and_attested_actor(flow):
    client, records, writer = flow
    path = f"/meeting-records/{uuid4()}"
    response = client.put(path, headers=AUTH, json=payload().model_dump(mode="json"))
    assert response.status_code == 202
    assert "transcript" not in response.text and "pending_payload" not in response.text
    current = client.get(path, headers=AUTH).json()
    assert current["persistence_status"] == "saved" and current["doc_id"]
    assert current["created_by"] == "env-bootstrap"
    assert client.put(path, headers=AUTH, json=payload().model_dump(mode="json")).status_code == 200
    changed = payload().model_dump(mode="json") | {"summary": "different"}
    assert client.put(path, headers=AUTH, json=changed).status_code == 409
    assert len(writer.created) == 1


def test_scope_and_validation_before_external_work(flow):
    client, records, writer = flow
    key, prefix, digest = generate_key()
    records.storage.create_api_key(
        name="read-only", prefix=prefix, key_hash=digest, scopes=["meetings:read"], actor="admin"
    )
    path = f"/meeting-records/{uuid4()}"
    assert (
        client.put(
            path, headers={"X-API-Key": key}, json=payload().model_dump(mode="json")
        ).status_code
        == 403
    )
    assert (
        client.put(
            path, headers=AUTH, json=payload().model_dump(mode="json") | {"actor": "forged"}
        ).status_code
        == 422
    )
    assert not writer.created and not records.storage.list_meetings()


@pytest.mark.parametrize("operation", ["ingest", "refetch", "refresh"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_owner_404_preserves_content_and_grant_ttl_until_independently_confirmed(
    flow, operation, confirmed
):
    client, records, writer = flow
    r, _ = records.accept(uuid4(), payload(), actor="meeting")
    records.advance(r.meeting_id)
    r = records.storage.get_meeting(r.meeting_id)
    before = records.storage.get_doc(r.doc_id)
    grants = records.storage.list_grants(r.doc_id)
    chunks = [
        make_chunk(text="The current Google body", source_title_hash=content_hash(before.title))
    ]
    records.storage.replace_doc_chunks(r.doc_id, chunks)
    # Due for source-access refresh without waiting in real time.
    records.storage.update_doc(
        r.doc_id,
        {
            "source_access_attempted_at": NOW - timedelta(days=1000),
            "source_access_synced_at": NOW - timedelta(days=1000),
        },
        actor="test",
    )
    writer.fail = "fetch"
    writer.state = "trashed" if confirmed else "inaccessible"
    if operation == "ingest":
        response = client.post("/docs", headers=AUTH, json={"url": r.doc_url})
        assert response.status_code in {200, 201}
    elif operation == "refetch":
        assert client.post(f"/docs/{r.doc_id}/refetch", headers=AUTH).status_code == 502
    else:
        assert client.post("/docs/source-access/refresh-due", headers=AUTH).status_code == 200
    doc = records.storage.get_doc(r.doc_id)
    if confirmed:
        assert doc.purged_at and not doc.active
        assert not records.storage.get_doc_content(r.doc_id)
        assert records.storage.list_grants(r.doc_id) == []
        assert records.storage.list_doc_chunks(r.doc_id) == []
    else:
        assert doc.content_snapshot == before.content_snapshot
        assert records.storage.get_doc_content(r.doc_id) == "The current Google body"
        assert records.storage.list_grants(r.doc_id) == grants
        assert records.storage.list_doc_chunks(r.doc_id) == chunks
        assert records.storage.get_meeting(r.meeting_id).persistence_status == "inaccessible"
