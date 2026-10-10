from uuid import UUID

from contracts.visibility import Actor, DENY, SEE_ALL, doc_visible

P1 = UUID("11111111-1111-1111-1111-111111111111")
P2 = UUID("22222222-2222-2222-2222-222222222222")
T1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
T2 = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _v(ctx, owner_p=None, owner_t=None, grants=()):
    return doc_visible(ctx, owning_person_id=owner_p, owning_team_id=owner_t, grants=grants)


def test_see_all_and_deny():
    assert _v(SEE_ALL) is True
    assert _v(DENY, grants=[("org", None)]) is False


def test_owner_person_and_team():
    actor = Actor(person_id=P1, team_ids=frozenset({T1}))
    assert _v(actor, owner_p=P1) is True
    assert _v(actor, owner_t=T1) is True
    assert _v(actor, owner_p=P2, owner_t=T2) is False


def test_org_grant_visible_to_any_actor():
    actor = Actor(person_id=P2, team_ids=frozenset())
    assert _v(actor, grants=[("org", None)]) is True


def test_person_and_team_grants():
    actor = Actor(person_id=P1, team_ids=frozenset({T1}))
    assert _v(actor, grants=[("person", P1)]) is True
    assert _v(actor, grants=[("person", P2)]) is False
    assert _v(actor, grants=[("team", T1)]) is True
    assert _v(actor, grants=[("team", T2)]) is False


def test_source_team_grant_requires_confirmed_google_membership():
    ordinary_only = Actor(person_id=P1, team_ids=frozenset({T1}))
    assert _v(ordinary_only, grants=[("team", T1, "google_drive")]) is False
    synced = Actor(
        person_id=P1,
        team_ids=frozenset({T1}),
        source_team_ids=frozenset({T1}),
    )
    assert _v(synced, grants=[("team", T1, "google_drive")]) is True


def test_no_match_is_hidden():
    actor = Actor(person_id=P1, team_ids=frozenset({T1}))
    assert _v(actor, owner_p=P2, grants=[("person", P2), ("team", T2)]) is False
