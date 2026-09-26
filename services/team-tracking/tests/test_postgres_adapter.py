from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from contracts.types import (
    PersonCreate,
    PersonIdentifierCreate,
    PersonIdentifierUpdate,
    PersonUpdate,
    TeamCreate,
    TeamMembershipCreate,
    TeamMembershipUpdate,
)
from src.storage.errors import UnknownParentTeamError
from src.storage.postgres import PostgresStorageAdapter


@pytest.fixture
def adapter(clean_db: Engine) -> PostgresStorageAdapter:
    return PostgresStorageAdapter(clean_db)


def test_readiness_query(adapter):
    assert adapter.is_ready() is True


def test_create_and_get_person(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="Alex", primary_email="alex@utmist.ca"),
        actor="test",
    )
    fetched = adapter.get_person(p.id)
    assert fetched is not None
    assert fetched.display_name == "Alex"
    assert fetched.primary_email == "alex@utmist.ca"


def test_email_uniqueness_case_insensitive(adapter):
    adapter.create_person(
        PersonCreate(display_name="A", primary_email="alex@utmist.ca"),
        actor="t",
    )
    with pytest.raises(ValueError):
        adapter.create_person(
            PersonCreate(display_name="B", primary_email="ALEX@UTMIST.CA"),
            actor="t",
        )


def test_update_person(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    updated = adapter.update_person(p.id, PersonUpdate(display_name="Alexandra"), actor="editor")
    assert updated is not None
    assert updated.display_name == "Alexandra"
    assert updated.updated_by == "editor"


def test_team_hierarchy(adapter):
    parent = adapter.create_team(TeamCreate(slug="events", label="Events"), actor="t")
    child = adapter.create_team(
        TeamCreate(slug="events.agi", label="AGI", parent_id=parent.id),
        actor="t",
    )
    assert child.parent_id == parent.id


def test_create_team_nonexistent_parent_raises_unknown_parent(adapter):
    """FK violation on parent_id must surface as UnknownParentTeamError, not a slug conflict."""
    from uuid import uuid4

    bogus = uuid4()
    with pytest.raises(UnknownParentTeamError) as exc:
        adapter.create_team(
            TeamCreate(slug="orphan", label="Orphan", parent_id=bogus),
            actor="t",
        )
    assert exc.value.parent_id == bogus


def test_create_team_duplicate_slug_still_raises_value_error(adapter):
    """Unique-violation on slug stays a plain ValueError (router -> 409)."""
    adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    with pytest.raises(ValueError) as exc:
        adapter.create_team(TeamCreate(slug="ops", label="Other"), actor="t")
    assert not isinstance(exc.value, UnknownParentTeamError)
    assert "slug already exists" in str(exc.value)


def test_role_kinds_seeded(adapter):
    kinds = adapter.list_role_kinds()
    assert {k.id for k in kinds} == {"executive", "director", "lead", "member"}


def test_membership_full_cycle(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    # Explicit past started_at keeps this independent of the DB vs local-clock
    # timezone boundary (server CURRENT_DATE can differ from Python date.today()).
    m = adapter.create_membership(
        TeamMembershipCreate(person_id=p.id, team_id=team.id, started_at=date(2024, 9, 1)),
        actor="t",
    )
    assert m.role_kind_id == "member"
    assert m.is_team_admin is False

    made_admin = adapter.update_membership(
        m.id, TeamMembershipUpdate(is_team_admin=True), actor="t"
    )
    assert made_admin is not None
    assert made_admin.is_team_admin is True

    # End in the past: ended_at < CURRENT_DATE is NOT "currently active", so it
    # drops out of active_only.
    end = date(2025, 4, 30)
    ended = adapter.end_membership(m.id, end, actor="t")
    assert ended is not None
    assert ended.ended_at == end

    active = adapter.list_memberships(team_id=team.id, active_only=True)
    assert len(active) == 0


def test_as_of_query(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    adapter.create_membership(
        TeamMembershipCreate(
            person_id=p.id,
            team_id=team.id,
            started_at=date(2024, 9, 1),
            ended_at=date(2025, 4, 30),
        ),
        actor="t",
    )
    assert len(adapter.list_memberships(as_of=date(2024, 12, 1))) == 1
    assert len(adapter.list_memberships(as_of=date(2025, 6, 1))) == 0


def test_is_team_admin_filter(adapter):
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


def test_pg_membership_bad_fk_raises_valueerror(adapter):
    """A bad person/team/role_kind FK raises ValueError (router -> 400), not a
    bare IntegrityError (which would surface as 500)."""
    from uuid import uuid4

    with pytest.raises(ValueError):
        adapter.create_membership(
            TeamMembershipCreate(person_id=uuid4(), team_id=uuid4()), actor="t"
        )


def test_pg_membership_update_bad_role_kind_raises_valueerror(adapter):
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    m = adapter.create_membership(TeamMembershipCreate(person_id=p.id, team_id=team.id), actor="t")
    with pytest.raises(ValueError):
        adapter.update_membership(m.id, TeamMembershipUpdate(role_kind_id="nope"), actor="t")


def test_pg_membership_update_into_overlap_raises_valueerror(adapter):
    """Extending ended_at via update into another active membership's range for
    the same (person, team) hits the EXCLUDE constraint -> ValueError (400),
    not a 500. Mirrors the in-memory update-side overlap check."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    a = adapter.create_membership(
        TeamMembershipCreate(
            person_id=p.id, team_id=team.id, started_at=date(2026, 1, 1), ended_at=date(2026, 3, 1)
        ),
        actor="t",
    )
    # B starts exactly when A ends -> allowed.
    adapter.create_membership(
        TeamMembershipCreate(person_id=p.id, team_id=team.id, started_at=date(2026, 3, 1)),
        actor="t",
    )
    with pytest.raises(ValueError, match="overlap"):
        adapter.update_membership(a.id, TeamMembershipUpdate(ended_at=date(2026, 4, 1)), actor="t")


def test_pg_membership_overlap_exclusion_raises_valueerror(adapter):
    """The temporal-overlap EXCLUDE constraint surfaces as a ValueError -> 400."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    adapter.create_membership(TeamMembershipCreate(person_id=p.id, team_id=team.id), actor="t")
    with pytest.raises(ValueError, match="overlap"):
        adapter.create_membership(TeamMembershipCreate(person_id=p.id, team_id=team.id), actor="t")


def test_pg_membership_same_day_readd_allowed(adapter):
    """daterange upper bound is exclusive: end then re-add same day is allowed."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    m = adapter.create_membership(
        TeamMembershipCreate(person_id=p.id, team_id=team.id, started_at=date(2026, 1, 1)),
        actor="t",
    )
    adapter.end_membership(m.id, date(2026, 3, 1), actor="t")
    again = adapter.create_membership(
        TeamMembershipCreate(person_id=p.id, team_id=team.id, started_at=date(2026, 3, 1)),
        actor="t",
    )
    assert again.id != m.id


def test_pg_membership_disjoint_history_allowed(adapter):
    """Non-overlapping historical memberships for the same person+team are fine."""
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


def test_pg_active_only_includes_future_dated(adapter):
    """active_only includes a future-dated membership (regression for #7)."""
    p = adapter.create_person(
        PersonCreate(display_name="A", primary_email="a@utmist.ca"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="ops", label="Ops"), actor="t")
    from datetime import timedelta

    adapter.create_membership(
        TeamMembershipCreate(
            person_id=p.id, team_id=team.id, ended_at=date.today() + timedelta(days=30)
        ),
        actor="t",
    )
    assert len(adapter.list_memberships(active_only=True)) == 1


_DEDUP_SQL = """
DO $$
DECLARE
    affected integer;
BEGIN
    LOOP
        WITH conflicts AS (
            SELECT DISTINCT a.id AS loser_id
            FROM team_memberships a
            JOIN team_memberships b
              ON a.person_id = b.person_id
             AND a.team_id = b.team_id
             AND a.id <> b.id
             AND daterange(a.started_at, COALESCE(a.ended_at, 'infinity'::date))
                 && daterange(b.started_at, COALESCE(b.ended_at, 'infinity'::date))
            WHERE (b.started_at, b.created_at, b.id) < (a.started_at, a.created_at, a.id)
        )
        UPDATE team_memberships t
        SET ended_at = t.started_at,
            updated_at = now(),
            updated_by = 'migration_007_dedup'
        FROM conflicts c
        WHERE t.id = c.loser_id
          AND (t.ended_at IS NULL OR t.ended_at <> t.started_at);
        GET DIAGNOSTICS affected = ROW_COUNT;
        EXIT WHEN affected = 0;
    END LOOP;
END $$;
"""

_OVERLAP_COUNT_SQL = """
SELECT count(*) FROM team_memberships a
JOIN team_memberships b
  ON a.person_id = b.person_id AND a.team_id = b.team_id AND a.id <> b.id
 AND daterange(a.started_at, COALESCE(a.ended_at, 'infinity'::date))
     && daterange(b.started_at, COALESCE(b.ended_at, 'infinity'::date))
"""


def test_pg_migration_backfill_dedups_overlaps(clean_db: Engine):
    """Reproduces migration 007's backfill: with the constraint dropped, insert
    two overlapping PAST-started active rows, run the dedup, and confirm no
    overlaps remain and the constraint can then be (re)created."""
    engine = clean_db
    adapter = PostgresStorageAdapter(engine)
    p = adapter.create_person(
        PersonCreate(display_name="Dup", primary_email="dup@x.com"), actor="t"
    )
    team = adapter.create_team(TeamCreate(slug="dupteam", label="Dup"), actor="t")

    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE team_memberships DROP CONSTRAINT team_memberships_no_overlap")
        )
        # Two overlapping, past-started, open-ended memberships — the exact shape
        # the plain-CURRENT_DATE approach fails to resolve.
        for start in ("2024-09-01", "2025-01-15"):
            conn.execute(
                text(
                    "INSERT INTO team_memberships "
                    "(person_id, team_id, role_kind_id, started_at, ended_at, created_by, updated_by)"
                    " VALUES (:pid, :tid, 'member', :start, NULL, 's', 's')"
                ),
                {"pid": p.id, "tid": team.id, "start": start},
            )
        pre = conn.execute(text(_OVERLAP_COUNT_SQL)).scalar_one()
        assert pre > 0  # they really do overlap before backfill

    with engine.begin() as conn:
        conn.execute(text(_DEDUP_SQL))
        post = conn.execute(text(_OVERLAP_COUNT_SQL)).scalar_one()
        assert post == 0  # dedup leaves no overlaps
        # Constraint can now be recreated (restore normal migrated state).
        conn.execute(
            text(
                "ALTER TABLE team_memberships ADD CONSTRAINT team_memberships_no_overlap "
                "EXCLUDE USING gist (person_id WITH =, team_id WITH =, "
                "daterange(started_at, COALESCE(ended_at, 'infinity'::date)) WITH &&)"
            )
        )

    # Exactly one survivor remains active for the (person, team).
    active = adapter.list_memberships(person_id=p.id, team_id=team.id, active_only=True)
    assert len(active) == 1
    assert active[0].started_at == date(2024, 9, 1)


def test_api_key_create_and_lookup_pg(adapter):
    key = adapter.create_api_key(
        name="discord-bot",
        prefix="tt_pg_1",
        key_hash="$argon2id$v=19$fake",
        scopes=["people:read"],
        actor="admin",
    )
    fetched = adapter.get_api_key_by_prefix("tt_pg_1")
    assert fetched is not None
    assert fetched.id == key.id
    assert adapter.get_api_key_hash("tt_pg_1") == "$argon2id$v=19$fake"


def test_api_key_name_conflict_pg(adapter):
    adapter.create_api_key(name="dup", prefix="tt_pg_x", key_hash="h", scopes=[], actor="admin")
    with pytest.raises(ValueError):
        adapter.create_api_key(name="dup", prefix="tt_pg_y", key_hash="h", scopes=[], actor="admin")


def test_revoke_and_touch_pg(adapter):
    key = adapter.create_api_key(
        name="bot-pg", prefix="tt_pg_r", key_hash="h", scopes=[], actor="admin"
    )
    adapter.touch_api_key_last_used(key.id)
    assert adapter.get_api_key_by_prefix("tt_pg_r").last_used_at is not None
    adapter.revoke_api_key(key.id, actor="admin")
    assert adapter.get_api_key_hash("tt_pg_r") is None


def test_pg_provider_seed_present(adapter):
    assert {p.id for p in adapter.list_providers()} >= {"discord", "github", "notion", "uoft_email"}
    assert adapter.get_provider("discord").label == "Discord"


def test_pg_identifier_crud_and_reverse_lookup(adapter):
    person = adapter.create_person(
        PersonCreate(display_name="Alex", primary_email="alex@utmist.ca"), actor="t"
    )
    pi = adapter.create_person_identifier(
        person.id,
        PersonIdentifierCreate(provider="discord", external_id="123", handle="alex"),
        actor="bot",
    )
    assert pi.external_id == "123"
    assert [i.id for i in adapter.list_person_identifiers(person.id)] == [pi.id]

    found = adapter.get_person_by_identifier("discord", "123")
    assert found is not None and found.id == person.id

    updated = adapter.update_person_identifier(
        person.id, "discord", PersonIdentifierUpdate(handle="new"), actor="t"
    )
    assert updated.handle == "new"

    assert adapter.delete_person_identifier(person.id, "discord") is True
    assert adapter.delete_person_identifier(person.id, "discord") is False
    assert adapter.get_person_by_identifier("discord", "123") is None


def test_pg_duplicate_provider_raises(adapter):
    person = adapter.create_person(
        PersonCreate(display_name="Alex", primary_email="alex@utmist.ca"), actor="t"
    )
    adapter.create_person_identifier(
        person.id, PersonIdentifierCreate(provider="discord", external_id="1"), actor="t"
    )
    with pytest.raises(ValueError, match="already has an identifier for provider"):
        adapter.create_person_identifier(
            person.id, PersonIdentifierCreate(provider="discord", external_id="2"), actor="t"
        )


def test_pg_external_id_owned_by_another_person_raises(adapter):
    person_a = adapter.create_person(
        PersonCreate(display_name="Alex", primary_email="alex@utmist.ca"), actor="t"
    )
    person_b = adapter.create_person(
        PersonCreate(display_name="Blair", primary_email="blair@utmist.ca"), actor="t"
    )
    adapter.create_person_identifier(
        person_a.id, PersonIdentifierCreate(provider="discord", external_id="1"), actor="t"
    )
    with pytest.raises(ValueError, match="linked to another person"):
        adapter.create_person_identifier(
            person_b.id, PersonIdentifierCreate(provider="discord", external_id="1"), actor="t"
        )


def _seed_person(adapter, email="a@x.com", name="A"):
    return adapter.create_person(PersonCreate(display_name=name, primary_email=email), actor="t")


def test_pg_add_multiple_emails_to_one_person(adapter):
    p = _seed_person(adapter, "p@x.com")
    adapter.add_person_email(p.id, "one@x.com", actor="t")
    adapter.add_person_email(p.id, "two@x.com", actor="t")
    emails = [i.external_id for i in adapter.list_person_identifiers(p.id) if i.provider == "email"]
    assert sorted(emails) == ["one@x.com", "two@x.com"]


def test_pg_add_email_normalizes(adapter):
    p = _seed_person(adapter, "p@x.com")
    adapter.add_person_email(p.id, "  MixEd@Case.COM ", actor="t")
    assert adapter.get_person_by_identifier("email", "mixed@case.com").id == p.id


def test_pg_add_email_idempotent(adapter):
    p = _seed_person(adapter, "p@x.com")
    first = adapter.add_person_email(p.id, "e@x.com", actor="t")
    again = adapter.add_person_email(p.id, "E@X.com", actor="t")
    assert again.id == first.id
    assert sum(i.provider == "email" for i in adapter.list_person_identifiers(p.id)) == 1


def test_pg_add_email_rejects_another_persons_identifier(adapter):
    a = _seed_person(adapter, "a@x.com", "A")
    b = _seed_person(adapter, "b@x.com", "B")
    adapter.add_person_email(a.id, "shared@x.com", actor="t")
    with pytest.raises(ValueError, match="email_registered_to_another"):
        adapter.add_person_email(b.id, "shared@x.com", actor="t")


def test_pg_add_email_rejects_another_persons_primary(adapter):
    _seed_person(adapter, "a@x.com", "A")
    b = _seed_person(adapter, "b@x.com", "B")
    with pytest.raises(ValueError, match="email_registered_to_another"):
        adapter.add_person_email(b.id, "A@X.com", actor="t")  # a's primary


def test_pg_add_email_own_primary_creates_identifier(adapter):
    p = _seed_person(adapter, "me@x.com")
    ident = adapter.add_person_email(p.id, "Me@X.com", actor="t")
    assert ident.provider == "email"
    assert ident.external_id == "me@x.com"
    email_idents = [i for i in adapter.list_person_identifiers(p.id) if i.provider == "email"]
    assert len(email_idents) == 1

    again = adapter.add_person_email(p.id, "Me@X.com", actor="t")
    assert again.id == ident.id
    email_idents = [i for i in adapter.list_person_identifiers(p.id) if i.provider == "email"]
    assert len(email_idents) == 1


def test_pg_add_person_email_uses_on_conflict_do_nothing_path(adapter):
    """Exercises the new on_conflict_do_nothing + re-read code path directly
    (item 1). A true concurrent same-person race isn't deterministically
    reproducible in a unit test, but repeat calls through the same insert
    statement must still return the same row idempotently."""
    p = _seed_person(adapter, "race@x.com")
    first = adapter.add_person_email(p.id, "shared-addr@x.com", actor="t")
    again = adapter.add_person_email(p.id, "shared-addr@x.com", actor="t")
    assert again.id == first.id
    email_idents = [i for i in adapter.list_person_identifiers(p.id) if i.provider == "email"]
    assert len(email_idents) == 1


def test_pg_add_person_email_rejects_blank(adapter):
    p = _seed_person(adapter, "p2@x.com")
    with pytest.raises(ValueError, match="email_must_not_be_empty"):
        adapter.add_person_email(p.id, "   ", actor="t")


def test_pg_generic_identifier_ops_reject_email_provider(adapter):
    p = _seed_person(adapter, "p@x.com")
    with pytest.raises(ValueError, match="email_not_addressable_by_provider"):
        adapter.create_person_identifier(
            p.id, PersonIdentifierCreate(provider="email", external_id="e@x.com"), actor="t"
        )
    with pytest.raises(ValueError, match="email_not_addressable_by_provider"):
        adapter.update_person_identifier(
            p.id, "email", PersonIdentifierUpdate(external_id="e@x.com"), actor="t"
        )
    with pytest.raises(ValueError, match="email_not_addressable_by_provider"):
        adapter.delete_person_identifier(p.id, "email")
