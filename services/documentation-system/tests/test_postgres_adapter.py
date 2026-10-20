import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, local
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError

from contracts.storage import DuplicateActiveUrl
from contracts.types import MAX_CHUNK_CHARS, DocIngest, SourceGrant
from contracts.visibility import DENY, SEE_ALL, Actor
from src.config import get_settings
from src.content import content_hash
from src.ingest import ingest_doc
from src.storage.postgres import PostgresStorageAdapter
from tests.chunk_storage_cases import ChunkStorageCases, make_chunk, make_doc
from tests.ingestion_retrieval_cases import IngestionRetrievalCases
from tests.retrieval_cases import RetrievalCases

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1", reason="set RUN_PG_TESTS=1 to run Postgres tests"
)


class TestDocChunks(ChunkStorageCases):
    pass


class TestRetrieval(RetrievalCases):
    pass


class TestIngestionRetrieval(IngestionRetrievalCases):
    def test_failed_insert_rolls_back_then_real_search_and_retry_recover(self, flow):
        doc = flow.ingest()
        before = flow.search()
        chunks = flow.store.list_doc_chunks(UUID(doc["id"]))
        flow.providers.documents[doc["url"]]["content"] = (
            "CPSIF replacement after storage recovery."
        )

        def fail_insert(conn, cursor, statement, parameters, context, executemany):
            # The real replacement has already deleted the old rows inside its
            # transaction. The API must keep the catalog write and roll back
            # the complete chunk replacement, then recover through /docs/index.
            if statement.startswith("INSERT INTO doc_chunks"):
                raise SQLAlchemyError("private insert diagnostics")

        engine = flow.store._engine
        event.listen(engine, "before_cursor_execute", fail_insert)
        try:
            flow.refetch(doc)
            assert flow.store.list_doc_chunks(UUID(doc["id"])) == chunks
            assert flow.search() == before
            result = flow.retry()
            assert result["failed"] == 1
            assert result["results"][0]["detail"] == "index write failed: SQLAlchemyError"
            assert flow.store.list_doc_chunks(UUID(doc["id"])) == chunks
        finally:
            event.remove(engine, "before_cursor_execute", fail_insert)
        assert flow.retry()["indexed"] == 1
        flow.citations(flow.search(), doc, "CPSIF replacement after storage recovery.")
        assert flow.retry()["attempted"] == 0

    @pytest.mark.parametrize("context", ["person", "channel"])
    def test_real_retrieval_sql_error_fails_closed_and_recovers(self, flow, context):
        doc = flow.ingest()

        def fail_search(conn, cursor, statement, parameters, context, executemany):
            if "eligible AS MATERIALIZED" in statement:
                raise SQLAlchemyError("private query diagnostics")

        engine = flow.store._engine
        event.listen(engine, "before_cursor_execute", fail_search)
        headers = flow.headers("channel") if context == "channel" else flow.headers()
        try:
            response = flow.client.post("/search", headers=headers, json={"query": "CPSIF"})
            assert response.status_code == 503
            assert response.json() == {"detail": "retrieval storage unavailable"}
        finally:
            event.remove(engine, "before_cursor_execute", fail_search)
        assert {h["doc_id"] for h in flow.search(context="channel")} == {doc["id"]}


@pytest.mark.parametrize("visibility", [SEE_ALL, DENY, None])
def test_retrieval_context_validation_precedes_database_access(adapter, monkeypatch, visibility):
    from tests.retrieval_cases import embedding

    def unexpected_connection():
        pytest.fail("unscoped retrieval must fail before opening a database connection")

    monkeypatch.setattr(adapter._engine, "connect", unexpected_connection)
    with pytest.raises(ValueError):
        adapter.search_chunks(
            query="CPSIF",
            embedding=embedding(),
            visibility=visibility,
            k=1,
            min_cosine=0.3,
        )


@pytest.mark.parametrize(
    "invalid",
    [
        make_chunk(2),
        make_chunk().model_copy(
            update={"chunk_text": "a" * (MAX_CHUNK_CHARS + 1), "end_offset": MAX_CHUNK_CHARS + 1}
        ),
    ],
)
def test_chunk_validation_precedes_database_access(adapter, monkeypatch, invalid):
    def unexpected_connection():
        pytest.fail("invalid chunks must fail before opening a database connection")

    monkeypatch.setattr(adapter._engine, "begin", unexpected_connection)
    with pytest.raises(ValueError):
        adapter.replace_doc_chunks(uuid4(), [invalid])


def test_chunk_insert_failure_rolls_back_replacement(adapter):
    doc = make_doc(adapter)
    original = [make_chunk()]
    adapter.replace_doc_chunks(doc.id, original)

    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO doc_chunks"):
            raise RuntimeError("simulated insert failure")

    event.listen(adapter._engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="simulated insert failure"):
            adapter.replace_doc_chunks(doc.id, [make_chunk(text="replacement")])
    finally:
        event.remove(adapter._engine, "before_cursor_execute", fail_insert)
    assert adapter.list_doc_chunks(doc.id) == original


@pytest.mark.parametrize("already_indexed", [False, True])
def test_concurrent_chunk_replacements_never_mix_sets(adapter, already_indexed):
    doc = make_doc(adapter)
    if already_indexed:
        adapter.replace_doc_chunks(doc.id, [make_chunk()])
    sets = [[make_chunk(0), make_chunk(1)], [make_chunk(text="replacement")]]
    barrier = Barrier(2)

    def replace(chunks):
        barrier.wait(timeout=5)
        return adapter.replace_doc_chunks(doc.id, chunks)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(replace, chunks) for chunks in sets]
        assert all(future.result(timeout=10) for future in futures)
    assert adapter.list_doc_chunks(doc.id) in sets


@pytest.mark.parametrize("cleanup", ["retirement", "source_gone"])
def test_cleanup_rechecks_after_a_concurrent_commit(adapter, cleanup):
    doc = make_doc(adapter)
    chunks = [make_chunk()]
    adapter.upsert_doc_content(
        doc.id,
        content_text=chunks[0].chunk_text,
        content_hash=chunks[0].source_content_hash,
        fetched_at=None,
    )
    adapter.replace_doc_chunks(doc.id, chunks)
    expected = adapter.get_doc_content_meta(doc.id)
    if cleanup == "retirement":
        adapter.update_doc(doc.id, {"active": False}, actor="test")
    started = Event()
    worker_pid = []
    worker = local()
    refreshed_at = datetime.now(timezone.utc)

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if getattr(worker, "cleaning", False) and not worker_pid:
            worker_pid.append(conn.connection.driver_connection.info.backend_pid)
            started.set()

    def clean():
        worker.cleaning = True
        if cleanup == "retirement":
            return adapter.clear_doc_chunks_if_unindexable(doc.id)
        return adapter.delete_doc_content(doc.id, expected=expected)

    engine = adapter._engine
    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as conn:
                # Leave the newer state uncommitted while cleanup starts.
                if cleanup == "retirement":
                    conn.execute(
                        text("UPDATE docs SET active = true WHERE id = :id"), {"id": doc.id}
                    )
                else:
                    conn.execute(
                        text("UPDATE doc_content SET fetched_at = :at WHERE doc_id = :id"),
                        {"at": refreshed_at, "id": doc.id},
                    )
                future = pool.submit(clean)
                assert started.wait(5)
                blocked = False
                deadline = monotonic() + 5
                while not future.done() and monotonic() < deadline:
                    with engine.connect() as observer:
                        blocked = observer.execute(
                            text(
                                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                            ),
                            {"pid": worker_pid[0]},
                        ).scalar_one()
                    if blocked:
                        break
                    sleep(0.01)
                assert blocked or future.done(), "cleanup never reached the database"
            assert future.result(timeout=5) is False
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)

    assert adapter.list_doc_chunks(doc.id) == chunks
    assert adapter.get_doc_content(doc.id) == chunks[0].chunk_text
    if cleanup == "retirement":
        assert adapter.get_doc(doc.id).active
    else:
        assert adapter.get_doc_content_meta(doc.id).fetched_at == refreshed_at


def test_old_title_writer_rechecks_after_new_title_and_index_commit(adapter):
    doc = make_doc(adapter)
    old = make_chunk()
    adapter.upsert_doc_content(
        doc.id, content_text=old.chunk_text, content_hash=old.source_content_hash, fetched_at=None
    )
    adapter.replace_doc_chunks(doc.id, [old])
    newer = make_chunk(source_title_hash=content_hash("Renamed"), embedding=[0.5] * 1536)
    started = Event()
    worker_pid = []
    worker = local()

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if getattr(worker, "indexing", False) and not worker_pid:
            worker_pid.append(conn.connection.driver_connection.info.backend_pid)
            started.set()

    def write_old():
        worker.indexing = True
        return adapter.replace_doc_chunks_if_current(
            doc.id, old.source_content_hash, [old], expected_title_hash=old.source_title_hash
        )

    engine = adapter._engine
    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE docs SET title = 'Renamed' WHERE id = :id"), {"id": doc.id}
                )
                adapter._write_chunks(conn, doc.id, [newer])
                future = pool.submit(write_old)
                assert started.wait(5)
                blocked = False
                deadline = monotonic() + 5
                while not future.done() and monotonic() < deadline:
                    with engine.connect() as observer:
                        blocked = observer.execute(
                            text(
                                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                            ),
                            {"pid": worker_pid[0]},
                        ).scalar_one()
                    if blocked:
                        break
                    sleep(0.01)
                assert blocked, "old index writer did not wait for the document lock"
            assert future.result(timeout=5) is False
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)

    assert adapter.list_doc_chunks(doc.id) == [newer]
    assert doc.id not in adapter.list_docs_for_indexing()


@pytest.fixture
def adapter():
    engine = create_engine(get_settings().database_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE doc_grants, doc_tags, docs, api_keys RESTART IDENTITY CASCADE"))
    return PostgresStorageAdapter(engine)


def test_readiness_query(adapter):
    assert adapter.is_ready() is True


def _mk(adapter, url="https://x.com", tags=None):
    return adapter.create_doc(
        url=url,
        url_normalized=url,
        source_id="web",
        title="X",
        description=None,
        owning_team_id=None,
        owning_team_label=None,
        owning_person_id=None,
        owning_person_label=None,
        content_snapshot=None,
        fetched_at=None,
        tags=tags or [],
        actor="tester",
    )


def test_create_get_and_dedup_lookup(adapter):
    d = _mk(adapter, tags=["a"])
    assert adapter.get_doc(d.id).tags == ["a"]
    assert adapter.get_doc_by_normalized_url("https://x.com").id == d.id


def test_soft_delete_and_filters(adapter):
    d = _mk(adapter)
    adapter.update_doc(d.id, {"active": False}, actor="t")
    assert adapter.list_docs(active_only=True) == []


def test_tag_add_remove_and_filter(adapter):
    d = _mk(adapter, tags=["x"])
    assert adapter.list_docs(tag="x")[0].id == d.id
    adapter.remove_tag(d.id, "x")
    assert adapter.list_docs(tag="x") == []


def test_sources_seeded(adapter):
    assert adapter.get_source("gdocs").url_patterns == ["docs.google.com/document"]


def test_api_key_roundtrip(adapter):
    k = adapter.create_api_key(
        name="bot", prefix="pfx98765", key_hash="h", scopes=["docs:read"], actor="cli"
    )
    assert adapter.get_api_key_hash("pfx98765") == "h"
    adapter.revoke_api_key(k.id, actor="cli")
    assert adapter.get_api_key_hash("pfx98765") is None


def test_add_tag_idempotent(adapter):
    d = _mk(adapter, tags=["x"])
    assert adapter.add_tag(d.id, "x") is True  # duplicate — must not raise
    assert adapter.get_doc(d.id).tags == ["x"]  # still exactly one
    assert adapter.add_tag(d.id, "y") is True
    assert set(adapter.get_doc(d.id).tags) == {"x", "y"}


def test_second_active_dup_insert_is_rejected(adapter):
    # Bug #11: the partial unique index (url_normalized WHERE active) forbids a
    # second active row for the same URL. create_doc surfaces this as
    # DuplicateActiveUrl (on_conflict_do_nothing -> no RETURNING row).
    _mk(adapter, url="https://dup.com")
    with pytest.raises(DuplicateActiveUrl):
        _mk(adapter, url="https://dup.com")


def test_get_by_normalized_url_prefers_active_over_inactive(adapter):
    # Bug #5: a soft-removed row must not shadow the live active row.
    first = _mk(adapter, url="https://dup.com")
    adapter.update_doc(first.id, {"active": False}, actor="t")  # soft-remove
    live = _mk(adapter, url="https://dup.com")  # now allowed again (index is partial)
    got = adapter.get_doc_by_normalized_url("https://dup.com")
    assert got is not None and got.id == live.id


def test_reingest_allowed_after_soft_remove(adapter):
    # The partial index exempts inactive rows, so a URL can be re-catalogued.
    first = _mk(adapter, url="https://re.com")
    adapter.update_doc(first.id, {"active": False}, actor="t")
    second = _mk(adapter, url="https://re.com")  # must not raise
    assert second.id != first.id


def test_ingest_race_fallback_merges_into_existing(adapter):
    # Simulate the read-then-insert race deterministically: the dedup read in
    # ingest step 1 sees nothing (row not yet visible), but create_doc in step 5
    # hits the unique index. create_doc raises DuplicateActiveUrl and ingest
    # falls back to merging into the now-existing active row (created=False).
    from test_ingest import FakeDirectory, FakeFetchers
    from contracts.fetcher import FetchResult

    winner = _mk(adapter, url="https://race.com", tags=["existing"])

    class _RaceAdapter:
        """Delegates to the real adapter but hides the existing active row from
        the FIRST dedup lookup, forcing ingest down the create/conflict path."""

        def __init__(self, real):
            self._real = real
            self._hidden = True

        def get_doc_by_normalized_url(self, url_normalized):
            if self._hidden:
                self._hidden = False  # only the initial dedup read is blinded
                return None
            return self._real.get_doc_by_normalized_url(url_normalized)

        def __getattr__(self, name):
            return getattr(self._real, name)

    race = _RaceAdapter(adapter)
    result = ingest_doc(
        DocIngest(url="https://race.com", tags=["new"]),
        storage=race,
        fetchers=FakeFetchers(result=FetchResult(title="R")),
        directory=FakeDirectory(),
        actor="bot",
    )
    assert result.created is False
    assert result.doc.id == winner.id
    assert set(result.doc.tags) == {"existing", "new"}  # merged, not duplicated
    # Still exactly one active row for the URL.
    active = [
        d for d in adapter.list_docs(active_only=True) if d.url_normalized == "https://race.com"
    ]
    assert len(active) == 1


_P1 = UUID("11111111-1111-1111-1111-111111111111")
_T1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def test_grants_and_visibility_pg(adapter):
    d = _mk(adapter, url="https://g.com")
    assert adapter.add_grant(d.id, grantee_type="person", grantee_id=_P1, actor="t") is True
    actor = Actor(person_id=_P1, team_ids=frozenset({_T1}))
    assert adapter.get_doc(d.id, visibility=actor).id == d.id
    other = Actor(person_id=UUID(int=9), team_ids=frozenset())
    assert adapter.get_doc(d.id, visibility=other) is None
    assert adapter.list_docs(visibility=DENY) == []
    assert len(adapter.list_docs(visibility=SEE_ALL)) >= 1


def test_org_grant_partial_unique_pg(adapter):
    d = _mk(adapter, url="https://o.com")
    assert adapter.add_grant(d.id, grantee_type="org", grantee_id=None, actor="t") is True
    assert (
        adapter.add_grant(d.id, grantee_type="org", grantee_id=None, actor="t") is True
    )  # idempotent
    assert len(adapter.list_grants(d.id)) == 1


def test_concurrent_source_grant_replacements_never_leave_union_pg(adapter):
    doc = _mk(adapter, url="https://concurrent-source-grants.com")
    assert adapter.list_grants(doc.id) == []
    now = datetime.now(timezone.utc)
    first_deleted, second_started, second_deleted = Event(), Event(), Event()
    release_first, release_second = Event(), Event()
    snapshot = local()
    second_pid = []

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if getattr(snapshot, "name", None) == "second" and not second_pid:
            second_pid.append(conn.connection.driver_connection.info.backend_pid)
            second_started.set()

    def after_execute(conn, cursor, statement, parameters, context, executemany):
        if not context.isdelete or context.compiled.statement.table.name != "doc_grants":
            return
        if getattr(snapshot, "name", None) == "first":
            first_deleted.set()
            assert release_first.wait(10), "first replacement was not released"
        elif getattr(snapshot, "name", None) == "second":
            second_deleted.set()
            assert release_second.wait(10), "second replacement was not released"

    def replace(name, person_id, synced_at):
        snapshot.name = name
        return adapter.replace_source_grants(
            doc.id,
            origin="google_drive",
            grants=[
                SourceGrant(
                    grantee_type="person",
                    grantee_id=person_id,
                    source_permission_id=name,
                    source_principal=f"{name}@example.com",
                    source_role="reader",
                )
            ],
            synced_at=synced_at,
            expires_at=synced_at + timedelta(hours=48),
            actor=name,
        )

    engine = adapter._engine
    event.listen(engine, "before_cursor_execute", before_execute)
    event.listen(engine, "after_cursor_execute", after_execute)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(replace, "first", _P1, now)
            try:
                assert first_deleted.wait(5)
                second = executor.submit(replace, "second", UUID(int=2), now + timedelta(minutes=1))
                assert second_started.wait(5)
                # Wait until B either blocks on A's document lock or (without the
                # fix) deletes the empty grant set before A has inserted anything.
                blocked = False
                deadline = monotonic() + 5
                while not second_deleted.is_set() and monotonic() < deadline:
                    with engine.connect() as conn:
                        blocked = conn.execute(
                            text(
                                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                            ),
                            {"pid": second_pid[0]},
                        ).scalar_one()
                    if blocked:
                        break
                    sleep(0.01)
                assert blocked or second_deleted.is_set(), "second replacement never reached DB"
                release_first.set()
                assert first.result(timeout=5) is True
                release_second.set()
                assert second.result(timeout=5) is True
            finally:
                release_first.set()
                release_second.set()
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)
        event.remove(engine, "after_cursor_execute", after_execute)

    assert [grant.grantee_id for grant in adapter.list_grants(doc.id)] == [UUID(int=2)]
    assert adapter.get_doc(doc.id).source_access_synced_at == now + timedelta(minutes=1)
    assert blocked is True


def test_replace_source_grants_preserves_manual_and_revokes_old_source_rows_pg(adapter):
    d = _mk(adapter, url="https://source-grants.com")
    now = datetime.now(timezone.utc)
    adapter.add_grant(d.id, grantee_type="person", grantee_id=_P1, actor="admin")
    adapter.replace_source_grants(
        d.id,
        origin="google_drive",
        grants=[
            SourceGrant(
                grantee_type="team",
                grantee_id=_T1,
                source_permission_id="permission-1",
                source_principal="team@example.com",
                source_role="reader",
                source_inherited_from=["folder-1"],
            )
        ],
        synced_at=now,
        expires_at=now + timedelta(hours=48),
        actor="sync",
    )
    ordinary_member = Actor(person_id=uuid4(), team_ids=frozenset({_T1}))
    synced_member = Actor(
        person_id=ordinary_member.person_id,
        team_ids=frozenset({_T1}),
        source_team_ids=frozenset({_T1}),
    )
    assert adapter.get_doc(d.id, visibility=ordinary_member) is None
    assert adapter.get_doc(d.id, visibility=synced_member) is not None
    adapter.replace_source_grants(
        d.id,
        origin="google_drive",
        grants=[],
        synced_at=now,
        expires_at=now + timedelta(hours=48),
        actor="sync",
    )
    grants = adapter.list_grants(d.id)
    assert [(g.origin, g.grantee_type, g.grantee_id) for g in grants] == [("manual", "person", _P1)]


def test_list_docs_batched_tag_hydration_matches_per_doc(adapter):
    """list_docs hydrates tags via a single batched query; the tags attached to
    each doc must be identical (same values, same tag-sorted order) to what
    get_doc returns per-doc, across a multi-doc, multi-tag catalog — including
    docs with no tags."""
    d1 = _mk(adapter, url="https://one.com", tags=["gamma", "alpha", "beta"])
    d2 = _mk(adapter, url="https://two.com", tags=["zeta"])
    d3 = _mk(adapter, url="https://three.com", tags=[])
    listed = {d.id: d for d in adapter.list_docs(active_only=True)}
    for doc_id in (d1.id, d2.id, d3.id):
        assert listed[doc_id].tags == adapter.get_doc(doc_id).tags
    assert listed[d1.id].tags == ["alpha", "beta", "gamma"]  # tag-sorted
    assert listed[d3.id].tags == []


def test_pg_list_docs_has_content_selects_exactly_the_contentless(adapter):
    """Postgres parity for the in-memory has_content partition: the correlated
    EXISTS must agree with the in-memory row-presence check, or backfill
    discovery finds a different set in prod than the fast suite proves."""
    with_text = _mk(adapter, url="https://has.com")
    without_text = _mk(adapter, url="https://missing.com")
    adapter.upsert_doc_content(with_text.id, content_text="body", content_hash="h", fetched_at=None)
    assert [d.id for d in adapter.list_docs(has_content=False)] == [without_text.id]
    assert [d.id for d in adapter.list_docs(has_content=True)] == [with_text.id]


def test_pg_list_docs_has_content_none_does_not_filter(adapter):
    with_text = _mk(adapter, url="https://has.com")
    without_text = _mk(adapter, url="https://missing.com")
    adapter.upsert_doc_content(with_text.id, content_text="body", content_hash="h", fetched_at=None)
    assert {d.id for d in adapter.list_docs()} == {with_text.id, without_text.id}


def test_pg_list_docs_has_content_keys_on_content_row_not_snapshot(adapter):
    """A content_snapshot with no doc_content row must still read as
    contentless — same rule the in-memory adapter enforces."""
    doc = _mk(adapter, url="https://snapshot-only.com")
    adapter.update_doc(doc.id, {"content_snapshot": "preview"}, actor="t")
    listed = adapter.list_docs(has_content=False)
    assert [d.id for d in listed] == [doc.id]
    assert listed[0].content_snapshot == "preview"


def test_pg_list_docs_has_content_composes_with_visibility(adapter):
    """The EXISTS clause is ANDed with the visibility clause, so the filter
    cannot be used to enumerate docs the actor cannot see."""
    mine = _mk(adapter, url="https://mine.com")
    _mk(adapter, url="https://not-mine.com")
    adapter.add_grant(mine.id, grantee_type="person", grantee_id=_P1, actor="t")
    actor = Actor(person_id=_P1, team_ids=frozenset())
    assert [d.id for d in adapter.list_docs(has_content=False, visibility=actor)] == [mine.id]
    assert adapter.list_docs(has_content=False, visibility=DENY) == []
    assert len(adapter.list_docs(has_content=False, visibility=SEE_ALL)) == 2


def test_pg_list_docs_has_content_composes_with_other_filters(adapter):
    _mk(adapter, url="https://untagged.com")
    tagged = _mk(adapter, url="https://tagged.com", tags=["x"])
    assert [d.id for d in adapter.list_docs(has_content=False, tag="x")] == [tagged.id]
    adapter.upsert_doc_content(tagged.id, content_text="body", content_hash="h", fetched_at=None)
    assert adapter.list_docs(has_content=False, tag="x") == []


def test_pg_upsert_doc_content_round_trips(adapter):
    doc = _mk(adapter)
    adapter.upsert_doc_content(
        doc.id, content_text="full body", content_hash="hash1", fetched_at=None
    )
    assert adapter.get_doc_content(doc.id) == "full body"


def test_pg_upsert_doc_content_updates_on_conflict(adapter):
    doc = _mk(adapter)
    adapter.upsert_doc_content(doc.id, content_text="first", content_hash="hash1", fetched_at=None)
    adapter.upsert_doc_content(doc.id, content_text="second", content_hash="hash2", fetched_at=None)
    assert adapter.get_doc_content(doc.id) == "second"


def test_pg_get_doc_content_none_when_absent(adapter):
    doc = _mk(adapter)
    assert adapter.get_doc_content(doc.id) is None


def test_pg_get_doc_content_withheld_from_stranger(adapter):
    doc = _mk(adapter)
    adapter.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    stranger = Actor(person_id=uuid4(), team_ids=frozenset())
    assert adapter.get_doc_content(doc.id, visibility=stranger) is None


def test_pg_get_doc_content_returned_to_granted_actor(adapter):
    doc = _mk(adapter)
    person_id = uuid4()
    adapter.add_grant(doc.id, grantee_type="person", grantee_id=person_id, actor="test")
    adapter.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    granted = Actor(person_id=person_id, team_ids=frozenset())
    assert adapter.get_doc_content(doc.id, visibility=granted) == "secret"


def test_pg_get_doc_content_denied_context_withholds(adapter):
    from contracts.visibility import DENY

    doc = _mk(adapter)
    adapter.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    assert adapter.get_doc_content(doc.id, visibility=DENY) is None


def test_pg_get_doc_content_scoped_to_requested_doc(adapter):
    visible = _mk(adapter, url="https://visible.com")
    secret = _mk(adapter, url="https://secret.com")
    person_id = uuid4()
    adapter.add_grant(visible.id, grantee_type="person", grantee_id=person_id, actor="t")
    adapter.upsert_doc_content(secret.id, content_text="secret", content_hash="h", fetched_at=None)
    actor = Actor(person_id=person_id, team_ids=frozenset())
    # The actor can see `visible` but NOT `secret`. A cross join would leak
    # `secret`'s content because *some* doc is visible to this actor.
    assert adapter.get_doc_content(secret.id, visibility=actor) is None


def test_pg_get_doc_content_meta_round_trips_hash_and_fetched_at(adapter):
    from datetime import datetime, timezone

    doc = _mk(adapter)
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    adapter.upsert_doc_content(
        doc.id, content_text="full body", content_hash="hash1", fetched_at=now
    )
    meta = adapter.get_doc_content_meta(doc.id)
    assert meta is not None
    assert meta.content_hash == "hash1"
    assert meta.fetched_at == now


def test_pg_get_doc_content_meta_none_when_absent(adapter):
    doc = _mk(adapter)
    assert adapter.get_doc_content_meta(doc.id) is None


def test_pg_get_doc_content_meta_withheld_from_stranger(adapter):
    doc = _mk(adapter)
    adapter.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    stranger = Actor(person_id=uuid4(), team_ids=frozenset())
    assert adapter.get_doc_content_meta(doc.id, visibility=stranger) is None


def test_pg_get_doc_content_meta_returned_to_granted_actor(adapter):
    doc = _mk(adapter)
    person_id = uuid4()
    adapter.add_grant(doc.id, grantee_type="person", grantee_id=person_id, actor="test")
    adapter.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    granted = Actor(person_id=person_id, team_ids=frozenset())
    meta = adapter.get_doc_content_meta(doc.id, visibility=granted)
    assert meta is not None
    assert meta.content_hash == "h"


def test_pg_get_doc_content_meta_denied_context_withholds(adapter):
    from contracts.visibility import DENY

    doc = _mk(adapter)
    adapter.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    assert adapter.get_doc_content_meta(doc.id, visibility=DENY) is None


def test_pg_get_doc_content_meta_scoped_to_requested_doc(adapter):
    visible = _mk(adapter, url="https://visible-meta.com")
    secret = _mk(adapter, url="https://secret-meta.com")
    person_id = uuid4()
    adapter.add_grant(visible.id, grantee_type="person", grantee_id=person_id, actor="t")
    adapter.upsert_doc_content(secret.id, content_text="secret", content_hash="h", fetched_at=None)
    actor = Actor(person_id=person_id, team_ids=frozenset())
    # The actor can see `visible` but NOT `secret`. A cross join would leak
    # `secret`'s content metadata because *some* doc is visible to this actor.
    assert adapter.get_doc_content_meta(secret.id, visibility=actor) is None


@pytest.mark.parametrize("owner_field", ["owning_person_id", "owning_team_id"])
def test_ownership_requires_valid_grant_and_source_revocation_hides_doc(adapter, owner_field):
    doc = _mk(adapter)
    adapter.update_doc(
        doc.id, {owner_field: _P1 if owner_field == "owning_person_id" else _T1}, actor="t"
    )
    adapter.upsert_doc_content(doc.id, content_text="secret", content_hash="hash", fetched_at=None)
    actor = Actor(_P1, frozenset({_T1}))
    assert adapter.get_doc(doc.id, visibility=actor) is None
    assert adapter.list_docs(visibility=actor) == []
    assert adapter.get_doc_content(doc.id, visibility=actor) is None
    assert adapter.get_doc_content_meta(doc.id, visibility=actor) is None

    now = datetime.now(timezone.utc)
    grant = SourceGrant(
        grantee_type="person",
        grantee_id=_P1,
        source_permission_id="p1",
        source_principal="one@example.com",
        source_role="reader",
    )
    for grants, visible in [([grant], True), ([], False)]:
        adapter.replace_source_grants(
            doc.id,
            origin="google_drive",
            grants=grants,
            synced_at=now,
            expires_at=now + timedelta(hours=48),
            actor="sync",
        )
        assert (adapter.get_doc(doc.id, visibility=actor) is not None) is visible
        assert bool(adapter.list_docs(visibility=actor)) is visible
        assert (adapter.get_doc_content(doc.id, visibility=actor) is not None) is visible
        assert (adapter.get_doc_content_meta(doc.id, visibility=actor) is not None) is visible

    adapter.add_grant(doc.id, grantee_type="person", grantee_id=_P1, actor="admin")
    assert adapter.get_doc(doc.id, visibility=actor) is not None
