from datetime import date, timedelta
from uuid import uuid4

import pytest

from conftest import build_seed_providers, build_seed_role_kinds
from contracts.types import (
    PersonCreate,
    PersonIdentifierCreate,
    PersonIdentifierUpdate,
    PersonUpdate,
    TeamCreate,
    TeamMembershipCreate,
    TeamMembershipUpdate,
    TeamUpdate,
)
from src.storage.in_memory import InMemoryStorageAdapter


@pytest.fixture
def adapter() -> InMemoryStorageAdapter:
    return InMemoryStorageAdapter(seed_role_kinds=build_seed_role_kinds())


@pytest.fixture
def seeded_adapter():
    return InMemoryStorageAdapter(
        seed_role_kinds=build_seed_role_kinds(), seed_providers=build_seed_providers()
    )


def test_create_and_get_person(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="Alex", primary_email="alex@utmist.ca"),
        actor="test",
    )
    fetched = adapter.get_person(p.id)
    assert fetched is not None
    assert fetched.display_name == "Alex"
    assert fetched.primary_email == "alex@utmist.ca"
    assert fetched.created_by == "test"


def test_email_uniqueness_case_insensitive(adapter):
    adapter.create_person(
        PersonCreate(display_name="Alex", primary_email="alex@utmist.ca"),
        actor="test",
    )
    with pytest.raises(ValueError):
        adapter.create_person(
            PersonCreate(display_name="Other", primary_email="ALEX@UTMIST.CA"),
            actor="test",
        )


def test_update_person(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="Alex", primary_email="alex@utmist.ca"),
        actor="test",
    )
    updated = adapter.update_person(p.id, PersonUpdate(display_name="Alexandra"), actor="editor")
    assert updated is not None
    assert updated.display_name == "Alexandra"
    assert updated.updated_by == "editor"


def test_create_and_list_teams(adapter):
    parent = adapter.create_team(TeamCreate(slug="events", label="Events"), actor="test")
    child = adapter.create_team(
        TeamCreate(
            slug="events.agi_workshop_2025",
            label="AGI Workshop 2025",
            parent_id=parent.id,
        ),
        actor="test",
    )
    teams = adapter.list_teams()
    assert {t.slug for t in teams} == {"events", "events.agi_workshop_2025"}
    assert child.parent_id == parent.id


def test_role_kinds_seeded(adapter):
    kinds = adapter.list_role_kinds()
    assert {k.id for k in kinds} == {"executive", "director", "lead", "member"}


def test_membership_create_defaults(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="Alex", primary_email="alex@utmist.ca"),
        actor="test",
    )
    t = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="test")
    m = adapter.create_membership(
        TeamMembershipCreate(person_id=p.id, team_id=t.id),
        actor="test",
    )
    assert m.role_kind_id == "member"
    assert m.started_at == date.today()
    assert m.ended_at is None
    assert m.is_team_admin is False


def test_membership_list_filters(adapter):
    p1 = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    p2 = adapter.create_person(
        PersonCreate(display_name="B", primary_email="b@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    other = adapter.create_team(TeamCreate(slug="ev", label="Events"), actor="t")
    adapter.create_membership(TeamMembershipCreate(person_id=p1.id, team_id=team.id), actor="t")
    adapter.create_membership(TeamMembershipCreate(person_id=p2.id, team_id=team.id), actor="t")
    adapter.create_membership(TeamMembershipCreate(person_id=p1.id, team_id=other.id), actor="t")
    assert len(adapter.list_memberships(team_id=team.id)) == 2
    assert len(adapter.list_memberships(person_id=p1.id)) == 2


def test_end_membership_sets_ended_at(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    m = adapter.create_membership(TeamMembershipCreate(person_id=p.id, team_id=team.id), actor="t")
    end_date = date.today() + timedelta(days=30)
    ended = adapter.end_membership(m.id, end_date, actor="t")
    assert ended is not None
    assert ended.ended_at == end_date
    # A FUTURE-dated ended_at means the membership is still currently active
    # (regression guard for the #7 active_only bug: it must NOT drop future rows).
    active = adapter.list_memberships(team_id=team.id, active_only=True)
    assert len(active) == 1


def test_as_of_date_filter(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    m = adapter.create_membership(
        TeamMembershipCreate(
            person_id=p.id,
            team_id=team.id,
            started_at=date(2024, 9, 1),
            ended_at=date(2025, 4, 30),
        ),
        actor="t",
    )
    assert m.started_at == date(2024, 9, 1)
    as_of_active = adapter.list_memberships(as_of=date(2025, 1, 15))
    assert len(as_of_active) == 1
    as_of_before = adapter.list_memberships(as_of=date(2024, 8, 1))
    assert len(as_of_before) == 0
    as_of_after = adapter.list_memberships(as_of=date(2025, 6, 1))
    assert len(as_of_after) == 0


def test_is_team_admin_filter(adapter):
    """Regression test for the DESIGN.md 'current team admins' query."""
    p1 = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    p2 = adapter.create_person(
        PersonCreate(display_name="B", primary_email="b@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    adapter.create_membership(
        TeamMembershipCreate(person_id=p1.id, team_id=team.id, is_team_admin=True),
        actor="t",
    )
    adapter.create_membership(
        TeamMembershipCreate(person_id=p2.id, team_id=team.id),
        actor="t",
    )
    admins = adapter.list_memberships(team_id=team.id, is_team_admin=True)
    non_admins = adapter.list_memberships(team_id=team.id, is_team_admin=False)
    assert len(admins) == 1
    assert len(non_admins) == 1


def test_fk_checks_on_membership_create(adapter):
    """Membership create fails cleanly when FKs don't exist."""

    with pytest.raises(ValueError):
        adapter.create_membership(
            TeamMembershipCreate(person_id=uuid4(), team_id=uuid4()),
            actor="t",
        )


def test_create_membership_rejects_overlap(adapter):
    """A second active membership overlapping an existing one for the same
    (person, team) is rejected (mirrors the Postgres EXCLUDE constraint)."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    adapter.create_membership(TeamMembershipCreate(person_id=p.id, team_id=team.id), actor="t")
    with pytest.raises(ValueError, match="overlap"):
        adapter.create_membership(TeamMembershipCreate(person_id=p.id, team_id=team.id), actor="t")


def test_create_membership_same_day_readd_allowed_in_memory(adapter):
    """Ending then re-adding on the same day does not overlap (exclusive upper)."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    m = adapter.create_membership(
        TeamMembershipCreate(person_id=p.id, team_id=team.id, started_at=date(2026, 1, 1)),
        actor="t",
    )
    adapter.end_membership(m.id, date(2026, 3, 1), actor="t")
    # Re-add starting the same day the previous one ended -> allowed.
    again = adapter.create_membership(
        TeamMembershipCreate(person_id=p.id, team_id=team.id, started_at=date(2026, 3, 1)),
        actor="t",
    )
    assert again.id != m.id


def test_create_membership_non_overlapping_history_allowed(adapter):
    """Two disjoint historical memberships for the same person+team are fine."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    adapter.create_membership(
        TeamMembershipCreate(
            person_id=p.id, team_id=team.id, started_at=date(2020, 9, 1), ended_at=date(2021, 5, 1)
        ),
        actor="t",
    )
    adapter.create_membership(
        TeamMembershipCreate(
            person_id=p.id, team_id=team.id, started_at=date(2023, 9, 1), ended_at=date(2024, 5, 1)
        ),
        actor="t",
    )
    assert len(adapter.list_memberships(person_id=p.id)) == 2


def test_active_only_includes_future_dated_in_memory(adapter):
    """active_only must include a membership whose ended_at is in the future."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    adapter.create_membership(
        TeamMembershipCreate(
            person_id=p.id, team_id=team.id, ended_at=date.today() + timedelta(days=30)
        ),
        actor="t",
    )
    assert len(adapter.list_memberships(active_only=True)) == 1


def test_update_membership_role_kind_validation(adapter):
    """Updating with a nonexistent role_kind fails."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    m = adapter.create_membership(TeamMembershipCreate(person_id=p.id, team_id=team.id), actor="t")
    with pytest.raises(ValueError):
        adapter.update_membership(
            m.id,
            TeamMembershipUpdate(role_kind_id="nonexistent"),
            actor="t",
        )


def test_get_person_by_email_case_insensitive(adapter):
    created = adapter.create_person(
        PersonCreate(display_name="Sam", primary_email="sam@utmist.ca"),
        actor="test",
    )
    found = adapter.get_person_by_email("SAM@utmist.ca")
    assert found is not None
    assert found.id == created.id
    assert adapter.get_person_by_email("missing@utmist.ca") is None


def test_get_team_by_slug(adapter):
    adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    found = adapter.get_team_by_slug("ops")
    assert found is not None
    assert found.label == "Ops"
    assert adapter.get_team_by_slug("nonexistent") is None


def test_get_role_kind(adapter):
    lead = adapter.get_role_kind("lead")
    assert lead is not None
    assert lead.label == "Lead"
    assert adapter.get_role_kind("nonexistent") is None


def test_update_person_email_conflict(adapter):
    """update_person with an email that conflicts with another person raises."""
    adapter.create_person(PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t")
    p2 = adapter.create_person(
        PersonCreate(display_name="B", primary_email="b@utmist.ca"), actor="t"
    )
    with pytest.raises(ValueError):
        adapter.update_person(p2.id, PersonUpdate(primary_email="A@UTMIST.CA"), actor="t")


def test_update_team_slug_conflict(adapter):
    """update_team with a slug that conflicts with another team raises."""
    adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    t2 = adapter.create_team(TeamCreate(slug="events", label="Events"), actor="t")
    from contracts.types import TeamUpdate

    with pytest.raises(ValueError):
        adapter.update_team(t2.id, TeamUpdate(slug="ops"), actor="t")


def test_end_membership_on_nonexistent_returns_none(adapter):
    """end_membership on a nonexistent id returns None, does not raise."""

    result = adapter.end_membership(uuid4(), date.today(), actor="t")
    assert result is None


def test_list_memberships_active_only_direct(adapter):
    """active_only filter returns only rows where ended_at is None."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    m_active = adapter.create_membership(
        TeamMembershipCreate(person_id=p.id, team_id=team.id), actor="t"
    )
    adapter.create_membership(
        TeamMembershipCreate(person_id=p.id, team_id=team.id, ended_at=date(2020, 1, 1)),
        actor="t",
    )
    all_rows = adapter.list_memberships()
    active_rows = adapter.list_memberships(active_only=True)
    assert len(all_rows) == 2
    assert len(active_rows) == 1
    assert active_rows[0].id == m_active.id


def test_api_key_create_and_lookup(adapter):
    key = adapter.create_api_key(
        name="discord-bot",
        prefix="tt_abc123",
        key_hash="$argon2id$v=19$fake_hash",
        scopes=["people:read", "memberships:write"],
        actor="admin",
    )
    assert key.name == "discord-bot"
    assert key.prefix == "tt_abc123"
    assert key.scopes == ["people:read", "memberships:write"]
    assert key.active is True

    fetched = adapter.get_api_key_by_prefix("tt_abc123")
    assert fetched is not None
    assert fetched.id == key.id

    h = adapter.get_api_key_hash("tt_abc123")
    assert h == "$argon2id$v=19$fake_hash"


def test_api_key_name_unique(adapter):
    adapter.create_api_key(name="bot", prefix="tt_a", key_hash="h1", scopes=[], actor="admin")
    with pytest.raises(ValueError):
        adapter.create_api_key(name="bot", prefix="tt_b", key_hash="h2", scopes=[], actor="admin")


def test_api_key_prefix_unique(adapter):
    adapter.create_api_key(name="a", prefix="tt_x", key_hash="h1", scopes=[], actor="admin")
    with pytest.raises(ValueError):
        adapter.create_api_key(name="b", prefix="tt_x", key_hash="h2", scopes=[], actor="admin")


def test_revoke_api_key(adapter):
    key = adapter.create_api_key(name="bot", prefix="tt_p", key_hash="h", scopes=[], actor="admin")
    revoked = adapter.revoke_api_key(key.id, actor="admin")
    assert revoked is not None
    assert revoked.active is False
    assert revoked.revoked_at is not None
    # get_api_key_hash returns None for revoked keys (auth path)
    assert adapter.get_api_key_hash("tt_p") is None
    # get_api_key_by_prefix still returns it (admin visibility)
    assert adapter.get_api_key_by_prefix("tt_p") is not None


def test_touch_api_key_last_used(adapter):
    key = adapter.create_api_key(name="bot", prefix="tt_l", key_hash="h", scopes=[], actor="admin")
    assert key.last_used_at is None
    adapter.touch_api_key_last_used(key.id)
    assert adapter.get_api_key_by_prefix("tt_l").last_used_at is not None


def test_touch_api_key_nonexistent_is_noop(adapter):
    # Must not raise
    adapter.touch_api_key_last_used(uuid4())


def test_list_api_keys_active_only(adapter):
    k1 = adapter.create_api_key(name="a", prefix="tt_a", key_hash="h", scopes=[], actor="admin")
    adapter.create_api_key(name="b", prefix="tt_b", key_hash="h", scopes=[], actor="admin")
    adapter.revoke_api_key(k1.id, actor="admin")
    assert len(adapter.list_api_keys()) == 2
    assert len(adapter.list_api_keys(active_only=True)) == 1


def _adapter():
    return InMemoryStorageAdapter(
        seed_role_kinds=build_seed_role_kinds(),
        seed_providers=build_seed_providers(),
    )


def _person(a):
    return a.create_person(
        PersonCreate(display_name="Alex", primary_email="alex@utmist.ca"), actor="t"
    )


def test_list_and_get_providers():
    a = _adapter()
    assert {p.id for p in a.list_providers()} == {
        "discord",
        "github",
        "notion",
        "uoft_email",
        "email",
    }
    assert a.get_provider("discord").label == "Discord"
    assert a.get_provider("nope") is None


def test_create_and_list_identifier():
    a = _adapter()
    p = _person(a)
    pi = a.create_person_identifier(
        p.id,
        PersonIdentifierCreate(provider="discord", external_id="123", handle="alex"),
        actor="bot",
    )
    assert pi.provider == "discord" and pi.external_id == "123"
    assert pi.created_by == "bot"
    assert [i.id for i in a.list_person_identifiers(p.id)] == [pi.id]


def test_duplicate_provider_for_person_raises():
    a = _adapter()
    p = _person(a)
    a.create_person_identifier(
        p.id, PersonIdentifierCreate(provider="discord", external_id="1"), actor="t"
    )
    with pytest.raises(ValueError):
        a.create_person_identifier(
            p.id, PersonIdentifierCreate(provider="discord", external_id="2"), actor="t"
        )


def test_external_id_owned_by_another_person_raises():
    a = _adapter()
    p1 = _person(a)
    p2 = a.create_person(PersonCreate(display_name="Bo", primary_email="bo@utmist.ca"), actor="t")
    a.create_person_identifier(
        p1.id, PersonIdentifierCreate(provider="discord", external_id="1"), actor="t"
    )
    with pytest.raises(ValueError):
        a.create_person_identifier(
            p2.id, PersonIdentifierCreate(provider="discord", external_id="1"), actor="t"
        )


def test_reverse_lookup_returns_person():
    a = _adapter()
    p = _person(a)
    a.create_person_identifier(
        p.id, PersonIdentifierCreate(provider="discord", external_id="999"), actor="t"
    )
    found = a.get_person_by_identifier("discord", "999")
    assert found is not None and found.id == p.id
    assert a.get_person_by_identifier("discord", "absent") is None


def test_update_and_delete_identifier():
    a = _adapter()
    p = _person(a)
    a.create_person_identifier(
        p.id, PersonIdentifierCreate(provider="discord", external_id="1", handle="old"), actor="t"
    )
    updated = a.update_person_identifier(
        p.id, "discord", PersonIdentifierUpdate(handle="new"), actor="t"
    )
    assert updated.handle == "new" and updated.external_id == "1"
    assert (
        a.update_person_identifier(p.id, "github", PersonIdentifierUpdate(handle="x"), actor="t")
        is None
    )
    assert a.delete_person_identifier(p.id, "discord") is True
    assert a.delete_person_identifier(p.id, "discord") is False


def test_update_person_identifier_external_id_collision_raises():
    """Updating external_id to a value owned by another person raises ValueError."""
    a = _adapter()
    p1 = _person(a)
    p2 = a.create_person(PersonCreate(display_name="Bo", primary_email="bo@utmist.ca"), actor="t")
    a.create_person_identifier(
        p1.id, PersonIdentifierCreate(provider="discord", external_id="1"), actor="t"
    )
    a.create_person_identifier(
        p2.id, PersonIdentifierCreate(provider="discord", external_id="2"), actor="t"
    )
    with pytest.raises(ValueError):
        a.update_person_identifier(
            p2.id, "discord", PersonIdentifierUpdate(external_id="1"), actor="t"
        )


def test_update_person_identifier_external_id_change_succeeds():
    """Updating external_id to a new unclaimed value succeeds and updates reverse lookup."""
    a = _adapter()
    p = _person(a)
    a.create_person_identifier(
        p.id, PersonIdentifierCreate(provider="discord", external_id="1"), actor="t"
    )
    updated = a.update_person_identifier(
        p.id, "discord", PersonIdentifierUpdate(external_id="2"), actor="t"
    )
    assert updated is not None
    assert updated.external_id == "2"
    found_by_new = a.get_person_by_identifier("discord", "2")
    assert found_by_new is not None and found_by_new.id == p.id
    found_by_old = a.get_person_by_identifier("discord", "1")
    assert found_by_old is None


def test_create_person_defaults_access_level_member(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    assert p.access_level == "member"


def test_create_person_persists_access_level(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="B", primary_email="b@utmist.ca", access_level="admin"),
        actor="t",
    )
    assert p.access_level == "admin"
    assert adapter.get_person(p.id).access_level == "admin"


def test_update_person_changes_access_level(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="C", primary_email="c@utmist.ca"), actor="t"
    )
    updated = adapter.update_person(p.id, PersonUpdate(access_level="superuser"), actor="t")
    assert updated.access_level == "superuser"


def _seed_person(adapter, email="a@b.com", name="A"):
    from contracts.types import PersonCreate

    return adapter.create_person(PersonCreate(display_name=name, primary_email=email), actor="t")


def test_add_multiple_emails_to_one_person(seeded_adapter):
    p = _seed_person(seeded_adapter, "p@x.com")
    seeded_adapter.add_person_email(p.id, "one@x.com", actor="t")
    seeded_adapter.add_person_email(p.id, "two@x.com", actor="t")
    emails = [
        i.external_id for i in seeded_adapter.list_person_identifiers(p.id) if i.provider == "email"
    ]
    assert sorted(emails) == ["one@x.com", "two@x.com"]


def test_add_email_normalizes(seeded_adapter):
    p = _seed_person(seeded_adapter, "p@x.com")
    seeded_adapter.add_person_email(p.id, "  MixEd@Case.COM ", actor="t")
    assert seeded_adapter.get_person_by_identifier("email", "mixed@case.com").id == p.id


def test_add_email_idempotent(seeded_adapter):
    p = _seed_person(seeded_adapter, "p@x.com")
    first = seeded_adapter.add_person_email(p.id, "e@x.com", actor="t")
    again = seeded_adapter.add_person_email(p.id, "E@X.com", actor="t")
    assert again.id == first.id
    assert sum(i.provider == "email" for i in seeded_adapter.list_person_identifiers(p.id)) == 1


def test_add_email_rejects_another_persons_identifier(seeded_adapter):
    import pytest

    a = _seed_person(seeded_adapter, "a@x.com", "A")
    b = _seed_person(seeded_adapter, "b@x.com", "B")
    seeded_adapter.add_person_email(a.id, "shared@x.com", actor="t")
    with pytest.raises(ValueError, match="email_registered_to_another"):
        seeded_adapter.add_person_email(b.id, "shared@x.com", actor="t")


def test_add_email_rejects_another_persons_primary(seeded_adapter):
    import pytest

    _seed_person(seeded_adapter, "a@x.com", "A")
    b = _seed_person(seeded_adapter, "b@x.com", "B")
    with pytest.raises(ValueError, match="email_registered_to_another"):
        seeded_adapter.add_person_email(b.id, "A@X.com", actor="t")  # a's primary


def test_add_email_own_primary_creates_identifier(seeded_adapter):
    p = _seed_person(seeded_adapter, "me@x.com")
    ident = seeded_adapter.add_person_email(p.id, "Me@X.com", actor="t")
    assert ident.provider == "email"
    assert ident.external_id == "me@x.com"
    email_idents = [
        i for i in seeded_adapter.list_person_identifiers(p.id) if i.provider == "email"
    ]
    assert len(email_idents) == 1

    again = seeded_adapter.add_person_email(p.id, "Me@X.com", actor="t")
    assert again.id == ident.id
    email_idents = [
        i for i in seeded_adapter.list_person_identifiers(p.id) if i.provider == "email"
    ]
    assert len(email_idents) == 1


def test_update_person_primary_email_normalized(seeded_adapter):
    """PATCHing primary_email to a mixed-case/whitespace address must normalize it,
    so it can't evade add_person_email's ownership check for the lowercased form."""
    a = _seed_person(seeded_adapter, "a@x.com", "A")
    seeded_adapter.update_person(a.id, PersonUpdate(primary_email="Alice@X.com"), actor="t")
    assert seeded_adapter.get_person(a.id).primary_email == "alice@x.com"

    b = _seed_person(seeded_adapter, "b@x.com", "B")
    with pytest.raises(ValueError, match="email_registered_to_another"):
        seeded_adapter.add_person_email(b.id, "alice@x.com", actor="t")


def test_add_person_email_rejects_blank(seeded_adapter):
    p = _seed_person(seeded_adapter, "p@x.com")
    with pytest.raises(ValueError, match="email_must_not_be_empty"):
        seeded_adapter.add_person_email(p.id, "   ", actor="t")


def test_generic_identifier_ops_reject_email_provider(seeded_adapter):
    import pytest

    from contracts.types import PersonIdentifierCreate, PersonIdentifierUpdate

    p = _seed_person(seeded_adapter, "p@x.com")
    with pytest.raises(ValueError, match="email_not_addressable_by_provider"):
        seeded_adapter.create_person_identifier(
            p.id, PersonIdentifierCreate(provider="email", external_id="e@x.com"), actor="t"
        )
    with pytest.raises(ValueError, match="email_not_addressable_by_provider"):
        seeded_adapter.update_person_identifier(
            p.id, "email", PersonIdentifierUpdate(external_id="e@x.com"), actor="t"
        )
    with pytest.raises(ValueError, match="email_not_addressable_by_provider"):
        seeded_adapter.delete_person_identifier(p.id, "email")


def test_channel_teams_replace_retire_clear(adapter):
    a = adapter.create_team(TeamCreate(slug="a", label="A"), actor="t")
    b = adapter.create_team(TeamCreate(slug="b", label="B"), actor="t")
    assert adapter.get_channel_teams("1", "2").team_ids == []

    set_ = adapter.replace_channel_teams("1", "2", [a.id, b.id], actor="bot")
    assert set_.team_ids == sorted([a.id, b.id])
    assert set_.updated_by == "bot"

    adapter.update_team(a.id, TeamUpdate(active=False), actor="t")
    after = adapter.get_channel_teams("1", "2")
    assert after.team_ids == [b.id]
    assert after.version != set_.version

    adapter.clear_channel_teams("1", "2")
    assert adapter.get_channel_teams("1", "2").team_ids == []
    adapter.clear_channel_teams("1", "2")


def test_channel_version_survives_out_of_order_retirement(adapter, monkeypatch):
    """Retiring B stamps an *earlier* timestamp than a later edit to A (the
    retire captured its time first but committed last). A timestamp-based
    version would stay put and hide the revocation; the content version can't."""
    import src.storage.in_memory as mod

    a = adapter.create_team(TeamCreate(slug="a", label="A"), actor="t")
    b = adapter.create_team(TeamCreate(slug="b", label="B"), actor="t")
    adapter.replace_channel_teams("1", "2", [a.id, b.id], actor="bot")
    t1, t2 = b.updated_at + timedelta(seconds=1), b.updated_at + timedelta(seconds=2)

    monkeypatch.setattr(mod, "_now", lambda: t2)
    adapter.update_team(a.id, TeamUpdate(label="A2"), actor="t")
    cached = adapter.get_channel_teams("1", "2")

    monkeypatch.setattr(mod, "_now", lambda: t1)
    adapter.update_team(b.id, TeamUpdate(active=False), actor="t")
    fresh = adapter.get_channel_teams("1", "2")

    assert fresh.team_ids == [a.id]
    assert fresh.version != cached.version


def test_channel_version_is_order_independent_content_hash():
    from contracts.types import ChannelTeams

    x, y = uuid4(), uuid4()
    make = lambda ids: ChannelTeams(guild_id="1", channel_id="2", team_ids=ids)  # noqa: E731
    assert make([x, y]).version == make([y, x]).version
    assert make([x]).version != make([x, y]).version
    assert make([]).version != make([x]).version


def test_channel_teams_rejects_inactive_and_keeps_config(adapter):
    a = adapter.create_team(TeamCreate(slug="a", label="A"), actor="t")
    b = adapter.create_team(TeamCreate(slug="b", label="B"), actor="t")
    adapter.replace_channel_teams("1", "2", [a.id], actor="bot")
    adapter.update_team(b.id, TeamUpdate(active=False), actor="t")
    for bad in (b.id, uuid4()):
        with pytest.raises(ValueError, match="unknown_or_inactive_team"):
            adapter.replace_channel_teams("1", "2", [a.id, bad], actor="bot")
    assert adapter.get_channel_teams("1", "2").team_ids == [a.id]
