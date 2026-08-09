from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from contracts.types import Source
from contracts.visibility import DENY, SEE_ALL, Actor
from src.storage.in_memory import InMemoryStorageAdapter


def _sources():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Source(
            id="web",
            label="Web",
            url_patterns=[],
            requires_auth=False,
            has_api=False,
            content_fetch_enabled=True,
            created_at=now,
            updated_at=now,
            created_by="system",
            updated_by="system",
        ),
    ]


@pytest.fixture
def store():
    return InMemoryStorageAdapter(seed_sources=_sources())


def _mk(store, url="https://x.com", tags=None):
    return store.create_doc(
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


def test_create_and_get_doc(store):
    d = _mk(store)
    assert store.get_doc(d.id).url == "https://x.com"
    assert d.created_by == "tester"


def test_get_by_normalized_url(store):
    _mk(store, url="https://x.com")
    assert store.get_doc_by_normalized_url("https://x.com") is not None
    assert store.get_doc_by_normalized_url("https://nope.com") is None


def test_get_by_normalized_url_prefers_active_over_older_inactive(store):
    # Bug #5: an older soft-removed row must not shadow the live active row.
    old = _mk(store, url="https://dup.com")
    store.update_doc(old.id, {"active": False}, actor="tester")  # soft-remove the older row
    live = _mk(store, url="https://dup.com")  # newer, active
    got = store.get_doc_by_normalized_url("https://dup.com")
    assert got is not None
    assert got.id == live.id  # prefers active even though it is newer


def test_get_by_normalized_url_active_tiebreak_earliest_created(store):
    # Among multiple active rows the canonical winner is the earliest created_at.
    first = _mk(store, url="https://tie.com")
    _mk(store, url="https://tie.com")  # in-memory has no unique constraint
    got = store.get_doc_by_normalized_url("https://tie.com")
    assert got is not None and got.id == first.id


def test_tags_roundtrip_and_dedup(store):
    d = _mk(store, tags=["a", "b"])
    assert set(store.get_doc(d.id).tags) == {"a", "b"}
    assert store.add_tag(d.id, "a") is True  # idempotent
    assert set(store.get_doc(d.id).tags) == {"a", "b"}
    store.add_tag(d.id, "c")
    assert "c" in store.get_doc(d.id).tags
    assert store.remove_tag(d.id, "a") is True
    assert "a" not in store.get_doc(d.id).tags


def test_soft_delete_and_active_only_filter(store):
    d = _mk(store)
    store.update_doc(d.id, {"active": False}, actor="tester")
    assert store.list_docs(active_only=True) == []
    assert len(store.list_docs(active_only=False)) == 1


def test_list_filters_by_tag_and_source(store):
    a = _mk(store, url="https://a.com", tags=["x"])
    _mk(store, url="https://b.com", tags=["y"])
    got = store.list_docs(tag="x")
    assert [d.id for d in got] == [a.id]


def test_api_key_lifecycle(store):
    k = store.create_api_key(
        name="bot", prefix="pfx12345", key_hash="h", scopes=["docs:read"], actor="cli"
    )
    assert store.get_api_key_hash("pfx12345") == "h"
    store.revoke_api_key(k.id, actor="cli")
    assert store.get_api_key_hash("pfx12345") is None


P1 = UUID("11111111-1111-1111-1111-111111111111")
T1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _mk_vis(a, url="https://x.com", owner_p=None, owner_t=None):
    return a.create_doc(
        url=url,
        url_normalized=url,
        source_id="web",
        title="X",
        description=None,
        owning_team_id=owner_t,
        owning_team_label=None,
        owning_person_id=owner_p,
        owning_person_label=None,
        content_snapshot="secret",
        fetched_at=None,
        tags=[],
        actor="t",
    )


def test_add_list_remove_grant():
    a = InMemoryStorageAdapter()
    d = _mk_vis(a)
    assert a.add_grant(d.id, grantee_type="org", grantee_id=None, actor="t") is True
    assert [(g.grantee_type, g.grantee_id) for g in a.list_grants(d.id)] == [("org", None)]
    # idempotent
    assert a.add_grant(d.id, grantee_type="org", grantee_id=None, actor="t") is True
    assert len(a.list_grants(d.id)) == 1
    assert a.remove_grant(d.id, grantee_type="org", grantee_id=None) is True
    assert a.list_grants(d.id) == []


def test_add_grant_missing_doc_returns_false():
    a = InMemoryStorageAdapter()
    assert a.add_grant(UUID(int=0), grantee_type="org", grantee_id=None, actor="t") is False


def test_get_doc_visibility_filters_and_hydrates_grants():
    a = InMemoryStorageAdapter()
    d = _mk_vis(a)  # no grants, no owner
    actor = Actor(person_id=P1, team_ids=frozenset({T1}))
    assert a.get_doc(d.id, visibility=actor) is None  # not visible
    assert a.get_doc(d.id, visibility=SEE_ALL).id == d.id  # see-all
    a.add_grant(d.id, grantee_type="person", grantee_id=P1, actor="t")
    got = a.get_doc(d.id, visibility=actor)
    assert got is not None
    assert ("person", P1) in [(g.grantee_type, g.grantee_id) for g in got.grants]


def test_list_docs_visibility():
    a = InMemoryStorageAdapter()
    owned = _mk_vis(a, url="https://owned", owner_p=P1)
    hidden = _mk_vis(a, url="https://hidden")
    actor = Actor(person_id=P1, team_ids=frozenset())
    ids = {d.id for d in a.list_docs(visibility=actor)}
    assert owned.id in ids and hidden.id not in ids
    assert a.list_docs(visibility=DENY) == []
    assert len(a.list_docs(visibility=SEE_ALL)) == 2


def test_upsert_doc_content_round_trips(store):
    doc = _mk(store)
    store.upsert_doc_content(
        doc.id, content_text="full body text", content_hash="hash1", fetched_at=None
    )
    assert store.get_doc_content(doc.id) == "full body text"


def test_upsert_doc_content_replaces_existing(store):
    doc = _mk(store)
    store.upsert_doc_content(doc.id, content_text="first", content_hash="hash1", fetched_at=None)
    store.upsert_doc_content(doc.id, content_text="second", content_hash="hash2", fetched_at=None)
    assert store.get_doc_content(doc.id) == "second"


def test_get_doc_content_none_when_no_content(store):
    doc = _mk(store)
    assert store.get_doc_content(doc.id) is None


def test_get_doc_content_none_for_unknown_doc(store):
    assert store.get_doc_content(uuid4()) is None


def test_get_doc_content_withheld_from_actor_who_cannot_see_doc(store):
    doc = _mk(store)
    store.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    stranger = Actor(person_id=uuid4(), team_ids=frozenset())
    assert store.get_doc_content(doc.id, visibility=stranger) is None


def test_get_doc_content_returned_to_granted_actor(store):
    doc = _mk(store)
    person_id = uuid4()
    store.add_grant(doc.id, grantee_type="person", grantee_id=person_id, actor="test")
    store.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    granted = Actor(person_id=person_id, team_ids=frozenset())
    assert store.get_doc_content(doc.id, visibility=granted) == "secret"


def test_get_doc_content_scoped_to_requested_doc(store):
    visible = _mk(store, url="https://visible.com")
    secret = _mk(store, url="https://secret.com")
    person_id = uuid4()
    store.add_grant(visible.id, grantee_type="person", grantee_id=person_id, actor="t")
    store.upsert_doc_content(secret.id, content_text="secret", content_hash="h", fetched_at=None)
    actor = Actor(person_id=person_id, team_ids=frozenset())
    # The actor can see `visible` but NOT `secret`. A cross join would leak
    # `secret`'s content because *some* doc is visible to this actor.
    assert store.get_doc_content(secret.id, visibility=actor) is None


def test_get_doc_content_meta_round_trips_hash_and_fetched_at(store):
    doc = _mk(store)
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    store.upsert_doc_content(
        doc.id, content_text="full body text", content_hash="hash1", fetched_at=now
    )
    meta = store.get_doc_content_meta(doc.id)
    assert meta is not None
    assert meta.content_hash == "hash1"
    assert meta.fetched_at == now


def test_get_doc_content_meta_none_when_no_content(store):
    doc = _mk(store)
    assert store.get_doc_content_meta(doc.id) is None


def test_get_doc_content_meta_none_for_unknown_doc(store):
    assert store.get_doc_content_meta(uuid4()) is None


def test_get_doc_content_meta_withheld_from_actor_who_cannot_see_doc(store):
    doc = _mk(store)
    store.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    stranger = Actor(person_id=uuid4(), team_ids=frozenset())
    assert store.get_doc_content_meta(doc.id, visibility=stranger) is None


def test_get_doc_content_meta_returned_to_granted_actor(store):
    doc = _mk(store)
    person_id = uuid4()
    store.add_grant(doc.id, grantee_type="person", grantee_id=person_id, actor="test")
    store.upsert_doc_content(doc.id, content_text="secret", content_hash="h", fetched_at=None)
    granted = Actor(person_id=person_id, team_ids=frozenset())
    meta = store.get_doc_content_meta(doc.id, visibility=granted)
    assert meta is not None
    assert meta.content_hash == "h"


def test_get_doc_content_meta_scoped_to_requested_doc(store):
    visible = _mk(store, url="https://visible-meta.com")
    secret = _mk(store, url="https://secret-meta.com")
    person_id = uuid4()
    store.add_grant(visible.id, grantee_type="person", grantee_id=person_id, actor="t")
    store.upsert_doc_content(secret.id, content_text="secret", content_hash="h", fetched_at=None)
    actor = Actor(person_id=person_id, team_ids=frozenset())
    # The actor can see `visible` but NOT `secret`. A cross join would leak
    # `secret`'s content metadata because *some* doc is visible to this actor.
    assert store.get_doc_content_meta(secret.id, visibility=actor) is None
