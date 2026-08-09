import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from contracts.storage import DuplicateActiveUrl
from contracts.types import DocIngest
from contracts.visibility import DENY, SEE_ALL, Actor
from src.config import get_settings
from src.ingest import ingest_doc
from src.storage.postgres import PostgresStorageAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1", reason="set RUN_PG_TESTS=1 to run Postgres tests"
)


@pytest.fixture
def adapter():
    engine = create_engine(get_settings().database_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE doc_grants, doc_tags, docs, api_keys RESTART IDENTITY CASCADE"))
    return PostgresStorageAdapter(engine)


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
