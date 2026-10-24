from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_providers, build_seed_role_kinds
from contracts.groups import ExternalMembersDisabled, GroupOperationPending, GroupProviderError
from contracts.types import PersonCreate, PersonUpdate, TeamCreate, TeamMembershipCreate, TeamUpdate
from src.api.app import create_app
from src.api.deps import get_group_provider, get_storage
from src.config import Settings, verify_production_secrets
from src.google_groups import sync_team, synced_groups_containing, synced_team_for_group
from src.providers.google_groups import GoogleGroupsProvider
from src.storage.in_memory import InMemoryStorageAdapter

AUTH = {"X-API-Key": "test-key"}


class FakeGroups:
    """In-memory Google Groups. Owners live outside `members`, as in Google."""

    domain = "utmist.example"

    def __init__(self) -> None:
        self.groups: dict[str, str] = {}  # email -> group name
        self.members: dict[str, dict[str, str]] = {}  # group name -> email -> membership
        self.owners: dict[str, set[str]] = {}
        self.external_allowed = True
        self.settings_calls: list[str] = []
        self.fail: str | None = None  # method name that raises GroupProviderError

    def _maybe_fail(self, method: str) -> None:
        if self.fail == method:
            raise GroupProviderError(f"{method}: HTTP 503 backend unavailable")

    def create_group(self, email, display_name, description):
        self._maybe_fail("create_group")
        if email in self.groups:
            raise AssertionError("duplicate group created")
        name = f"groups/{len(self.groups)}"
        self.groups[email] = name
        self.members[name] = {}
        self.owners[name] = {"infrastructure@utmist.example"}
        return name

    def list_members(self, group_name):
        self._maybe_fail("list_members")
        return dict(self.members[group_name])

    def ensure_external_members(self, group_email):
        assert group_email in self.groups
        self.settings_calls.append(group_email)
        self._maybe_fail("settings")

    def add_member(self, group_name, email):
        self._maybe_fail("add_member")
        if not self.external_allowed and not email.endswith("@" + self.domain):
            raise ExternalMembersDisabled("outside organization")
        self.members[group_name][email] = f"{group_name}/memberships/{email}"

    def remove_member(self, membership_name):
        self._maybe_fail("remove_member")
        for members in self.members.values():
            for email, name in list(members.items()):
                if name == membership_name:
                    del members[email]


@pytest.fixture
def storage():
    return InMemoryStorageAdapter(
        seed_role_kinds=build_seed_role_kinds(), seed_providers=build_seed_providers()
    )


@pytest.fixture
def groups():
    return FakeGroups()


def _team(storage, slug="ml"):
    return storage.create_team(TeamCreate(slug=slug, label=slug.upper()), actor="t")


def _member(storage, team, email, **kw):
    person = storage.create_person(PersonCreate(display_name=email, primary_email=email), actor="t")
    storage.create_membership(
        TeamMembershipCreate(person_id=person.id, team_id=team.id, **kw), actor="t"
    )
    return person


def _emails(groups, state):
    return set(groups.members[state.group_name])


# --- Reconciler ---


def test_creates_group_and_adds_primary_and_verified_emails(storage, groups):
    team = _team(storage)
    ada = _member(storage, team, "ada@gmail.com")
    storage.add_person_email(ada.id, "Ada@Alumni.ca", actor="t")

    state = sync_team(storage, groups, team.id, actor="tester")

    assert state.status == "synced"
    assert state.group_email == "ml@utmist.example"
    assert state.last_synced_at is not None and state.updated_by == "tester"
    assert _emails(groups, state) == {"ada@gmail.com", "ada@alumni.ca"}
    assert storage.get_team_google_group(team.id) == state


def test_resync_is_idempotent_and_never_duplicates_the_group(storage, groups):
    team = _team(storage)
    _member(storage, team, "ada@gmail.com")
    first = sync_team(storage, groups, team.id, actor="t")
    second = sync_team(storage, groups, team.id, actor="t")
    assert first.group_name == second.group_name
    assert len(groups.groups) == 1


def test_removes_ended_inactive_and_future_members_but_not_owners(storage, groups):
    team = _team(storage)
    stays = _member(storage, team, "stays@gmail.com")
    leaves = _member(storage, team, "leaves@gmail.com")
    retired = _member(storage, team, "retired@gmail.com")
    _member(storage, team, "later@gmail.com", started_at=date.today() + timedelta(days=3))
    state = sync_team(storage, groups, team.id, actor="t")
    assert _emails(groups, state) == {
        "stays@gmail.com",
        "leaves@gmail.com",
        "retired@gmail.com",
    }

    (m,) = storage.list_memberships(person_id=leaves.id)
    storage.end_membership(m.id, date.today(), actor="t")
    storage.update_person(retired.id, PersonUpdate(active=False), actor="t")
    state = sync_team(storage, groups, team.id, actor="t")

    assert _emails(groups, state) == {stays.primary_email}
    assert groups.owners[state.group_name] == {"infrastructure@utmist.example"}


def test_retired_team_keeps_group_but_loses_every_member(storage, groups):
    team = _team(storage)
    _member(storage, team, "ada@gmail.com")
    sync_team(storage, groups, team.id, actor="t")
    storage.update_team(team.id, TeamUpdate(active=False), actor="t")
    state = sync_team(storage, groups, team.id, actor="t")
    assert state.status == "synced" and _emails(groups, state) == set()


def test_retired_team_without_group_is_skipped(storage, groups):
    team = _team(storage)
    storage.update_team(team.id, TeamUpdate(active=False), actor="t")
    assert sync_team(storage, groups, team.id, actor="t") is None
    assert groups.groups == {}


def test_unknown_team_returns_none(storage, groups):
    assert sync_team(storage, groups, uuid4(), actor="t") is None


def test_external_members_disabled_is_visible_then_recoverable(storage, groups):
    team = _team(storage)
    _member(storage, team, "ada@gmail.com")
    _member(storage, team, "bob@utmist.example")
    groups.external_allowed = False

    state = sync_team(storage, groups, team.id, actor="t")
    assert state.status == "needs_external_members"
    assert "Allow external members" in state.last_error
    assert state.last_synced_at is None
    assert _emails(groups, state) == {"bob@utmist.example"}

    groups.external_allowed = True
    state = sync_team(storage, groups, team.id, actor="t")
    assert state.status == "synced" and state.last_error is None
    assert _emails(groups, state) == {"ada@gmail.com", "bob@utmist.example"}


def test_failing_addition_does_not_block_revocation(storage, groups):
    team = _team(storage)
    leaves = _member(storage, team, "leaves@gmail.com")
    state = sync_team(storage, groups, team.id, actor="t")
    (m,) = storage.list_memberships(person_id=leaves.id)
    storage.end_membership(m.id, date.today(), actor="t")
    _member(storage, team, "rejected@gmail.com")

    real_add = groups.add_member

    def add_member(group_name, email):
        if email == "rejected@gmail.com":
            raise GroupProviderError("add member rejected@gmail.com: HTTP 400 invalid")
        real_add(group_name, email)

    groups.add_member = add_member
    state = sync_team(storage, groups, team.id, actor="t")

    assert _emails(groups, state) == set()
    assert state.status == "failed" and "rejected@gmail.com" in state.last_error


def test_external_block_alongside_another_failure_is_failed_with_both(storage, groups):
    team = _team(storage)
    _member(storage, team, "ada@gmail.com")
    state = sync_team(storage, groups, team.id, actor="t")
    stale = f"{state.group_name}/memberships/gone@gmail.com"
    groups.members[state.group_name]["gone@gmail.com"] = stale
    groups.members[state.group_name].pop("ada@gmail.com")
    groups.external_allowed = False

    def remove_member(membership_name):
        raise GroupProviderError("remove member: HTTP 503 unavailable")

    groups.remove_member = remove_member
    state = sync_team(storage, groups, team.id, actor="t")
    assert state.status == "failed"
    assert "503" in state.last_error and "Allow external members" in state.last_error


def test_failed_creation_is_recorded_and_retry_succeeds(storage, groups):
    team = _team(storage)
    _member(storage, team, "ada@gmail.com")
    groups.fail = "create_group"
    state = sync_team(storage, groups, team.id, actor="t")
    assert state.status == "failed" and state.group_name is None
    assert "503" in state.last_error

    groups.fail = None
    state = sync_team(storage, groups, team.id, actor="t")
    assert state.status == "synced" and _emails(groups, state) == {"ada@gmail.com"}


def test_address_follows_slug_until_the_group_exists(storage, groups):
    team = _team(storage, "taken")
    groups.fail = "create_group"
    sync_team(storage, groups, team.id, actor="t")
    storage.update_team(team.id, TeamUpdate(slug="free"), actor="t")
    groups.fail = None
    state = sync_team(storage, groups, team.id, actor="t")
    assert state.group_email == "free@utmist.example"

    storage.update_team(team.id, TeamUpdate(slug="renamed"), actor="t")
    assert sync_team(storage, groups, team.id, actor="t").group_email == "free@utmist.example"


def test_membership_failure_after_success_keeps_last_synced_at(storage, groups):
    team = _team(storage)
    ok = sync_team(storage, groups, team.id, actor="t")
    groups.fail = "list_members"
    failed = sync_team(storage, groups, team.id, actor="t")
    assert failed.status == "failed"
    assert failed.group_name == ok.group_name
    assert failed.last_synced_at == ok.last_synced_at


def test_put_unknown_team_raises(storage):
    with pytest.raises(ValueError, match="team_id not found"):
        storage.put_team_google_group(
            uuid4(),
            group_email="x@y",
            group_name=None,
            status="failed",
            last_error=None,
            last_synced_at=None,
            actor="t",
        )


# --- HTTP surface ---


@pytest.fixture
def client(monkeypatch, storage, groups):
    monkeypatch.setenv("API_KEY", "test-key")
    from src.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_group_provider] = lambda: groups
    with TestClient(app) as c:
        yield c


def test_api_writes_keep_the_group_in_sync(client, groups):
    team = client.post("/teams", json={"slug": "ml", "label": "ML"}, headers=AUTH).json()
    got = client.get(f"/teams/{team['id']}/google-group", headers=AUTH)
    assert got.status_code == 200 and got.json()["group_email"] == "ml@utmist.example"
    name = got.json()["group_name"]

    person = client.post(
        "/people", json={"display_name": "Ada", "primary_email": "ada@gmail.com"}, headers=AUTH
    ).json()
    m = client.post(
        "/memberships", json={"person_id": person["id"], "team_id": team["id"]}, headers=AUTH
    ).json()
    assert set(groups.members[name]) == {"ada@gmail.com"}

    client.post(f"/people/{person['id']}/emails", json={"email": "ada@alumni.ca"}, headers=AUTH)
    assert set(groups.members[name]) == {"ada@gmail.com", "ada@alumni.ca"}

    client.patch(f"/people/{person['id']}", json={"primary_email": "ada@new.ca"}, headers=AUTH)
    assert set(groups.members[name]) == {"ada@new.ca", "ada@alumni.ca"}

    client.post(f"/memberships/{m['id']}/end", json={"ended_at": str(date.today())}, headers=AUTH)
    assert groups.members[name] == {}

    listed = client.get("/teams/google-groups", headers=AUTH)
    assert [g["team_id"] for g in listed.json()] == [team["id"]]


@pytest.mark.parametrize("failure", ["create_group", "settings"])
def test_sync_endpoint(client, groups, failure):
    groups.fail = failure
    team = client.post("/teams", json={"slug": "ml", "label": "ML"}, headers=AUTH).json()
    assert (
        client.get(f"/teams/{team['id']}/google-group", headers=AUTH).json()["status"] == "failed"
    )
    groups.fail = None
    resp = client.post(f"/teams/{team['id']}/google-group/sync", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["status"] == "synced"
    assert client.post(f"/teams/{uuid4()}/google-group/sync", headers=AUTH).status_code == 404
    assert client.get(f"/teams/{uuid4()}/google-group", headers=AUTH).status_code == 404


def test_unconfigured_provider_skips_sync_and_endpoint_503s(client):
    client.app.dependency_overrides[get_group_provider] = lambda: None
    team = client.post("/teams", json={"slug": "ml", "label": "ML"}, headers=AUTH).json()
    assert client.get(f"/teams/{team['id']}/google-group", headers=AUTH).status_code == 404
    assert client.post(f"/teams/{team['id']}/google-group/sync", headers=AUTH).status_code == 503


def test_team_lookup_by_group_requires_synced_status(client, storage, groups):
    team = client.post("/teams", json={"slug": "ml", "label": "ML"}, headers=AUTH).json()
    url = "/teams/by-synced-google-group/ML@utmist.example"
    got = client.get(url, headers=AUTH)
    assert got.status_code == 200 and got.json()["id"] == team["id"]

    state = storage.get_team_google_group(UUID(team["id"]))
    for bad in ("needs_external_members", "failed"):
        storage.put_team_google_group(
            state.team_id,
            group_email=state.group_email,
            group_name=state.group_name,
            status=bad,
            last_error="x",
            last_synced_at=None,
            actor="t",
        )
        assert client.get(url, headers=AUTH).status_code == 404

    client.post(f"/teams/{team['id']}/google-group/sync", headers=AUTH)
    client.patch(f"/teams/{team['id']}", json={"active": False}, headers=AUTH)
    retired = client.get(url, headers=AUTH)
    assert retired.status_code == 200 and retired.json()["active"] is False
    assert client.get("/teams/by-synced-google-group/x@y", headers=AUTH).status_code == 404


def test_synced_google_teams_reads_google_live(client, storage, groups):
    ml = client.post("/teams", json={"slug": "ml", "label": "ML"}, headers=AUTH).json()
    client.post("/teams", json={"slug": "ops", "label": "Ops"}, headers=AUTH)
    person = client.post(
        "/people", json={"display_name": "Ada", "primary_email": "ada@gmail.com"}, headers=AUTH
    ).json()
    client.post("/memberships", json={"person_id": person["id"], "team_id": ml["id"]}, headers=AUTH)
    url = f"/people/{person['id']}/synced-google-teams"
    assert [g["team_id"] for g in client.get(url, headers=AUTH).json()] == [ml["id"]]

    # Google is the authority: a verified alternate address counts, and a
    # member removed on Google's side is gone even though the directory says otherwise.
    name = storage.get_team_google_group(UUID(ml["id"])).group_name
    groups.members[name] = {"ada@alumni.ca": "m/1"}
    storage.add_person_email(UUID(person["id"]), "ada@alumni.ca", actor="t")
    assert [g["team_id"] for g in client.get(url, headers=AUTH).json()] == [ml["id"]]
    groups.members[name] = {}
    assert client.get(url, headers=AUTH).json() == []

    groups.fail = "list_members"
    assert client.get(url, headers=AUTH).status_code == 503
    assert client.get(f"/people/{uuid4()}/synced-google-teams", headers=AUTH).status_code == 404
    client.app.dependency_overrides[get_group_provider] = lambda: None
    assert client.get(url, headers=AUTH).status_code == 503


def test_retired_team_sync_endpoint_409(client):
    client.app.dependency_overrides[get_group_provider] = lambda: None
    team = client.post("/teams", json={"slug": "ml", "label": "ML"}, headers=AUTH).json()
    client.patch(f"/teams/{team['id']}", json={"active": False}, headers=AUTH)
    client.app.dependency_overrides[get_group_provider] = FakeGroups
    assert client.post(f"/teams/{team['id']}/google-group/sync", headers=AUTH).status_code == 409


# --- Google provider (HTTP mapping) ---


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class ReadySettingsSession:
    """Existing membership tests have independently verified group settings."""

    def request(self, method, url, **kwargs):
        from urllib.parse import unquote

        assert method == "GET"
        return FakeResponse(
            200,
            {
                "email": unquote(url.rsplit("/", 1)[1]),
                "allowExternalMembers": "true",
                "whoCanAddExternalMembers": "END_USERS_CAN_ADD_EXTERNAL_MEMBERS",
            },
        )


def _provider(*responses):
    session = FakeSession(*responses)
    p = GoogleGroupsProvider(
        customer_id="C01",
        domain="utmist.example",
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        session=session,
        settings_session=ReadySettingsSession(),
    )
    return p, session


def test_provider_create_group_returns_resource_name():
    p, s = _provider(FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}))
    assert p.create_group("ml@utmist.example", "ML", "marker") == "groups/abc"
    method, url, kw = s.calls[0]
    assert url.endswith("/v1/groups") and kw["json"]["parent"] == "customers/C01"
    assert kw["params"] == {"initialGroupConfig": "WITH_INITIAL_OWNER"}


def test_provider_adopts_own_group_on_conflict_but_not_a_stranger():
    p, _ = _provider(
        FakeResponse(409, {"error": {"message": "exists"}}),
        FakeResponse(200, {"name": "groups/abc"}),
        FakeResponse(200, {"name": "groups/abc", "description": "marker"}),
    )
    assert p.create_group("ml@utmist.example", "ML", "marker") == "groups/abc"

    p, _ = _provider(
        FakeResponse(409, {}),
        FakeResponse(200, {"name": "groups/xyz"}),
        FakeResponse(200, {"name": "groups/xyz", "description": "someone else's"}),
    )
    with pytest.raises(GroupProviderError, match="already in use"):
        p.create_group("exec@utmist.example", "Exec", "marker")


def test_provider_unfinished_operation_is_an_error():
    p, _ = _provider(FakeResponse(200, {"done": False}))
    with pytest.raises(GroupProviderError, match="not finished"):
        p.create_group("ml@utmist.example", "ML", "marker")


def test_provider_lists_only_plain_members_across_pages():
    p, s = _provider(
        FakeResponse(
            200,
            {
                "memberships": [
                    {
                        "name": "m/1",
                        "preferredMemberKey": {"id": "Ada@x"},
                        "roles": [{"name": "MEMBER"}],
                    },
                    {
                        "name": "m/2",
                        "preferredMemberKey": {"id": "owner@x"},
                        "roles": [{"name": "MEMBER"}, {"name": "OWNER"}],
                    },
                ],
                "nextPageToken": "t",
            },
        ),
        FakeResponse(
            200,
            {
                "memberships": [
                    {
                        "name": "m/3",
                        "preferredMemberKey": {"id": "bob@x"},
                        "roles": [{"name": "MEMBER"}],
                    }
                ]
            },
        ),
    )
    assert p.list_members("groups/abc") == {"ada@x": "m/1", "bob@x": "m/3"}
    assert s.calls[1][2]["params"]["pageToken"] == "t"


def test_provider_add_member_outcomes():
    p, _ = _provider(
        FakeResponse(200, {"done": True}),
        FakeResponse(409, {}),
        FakeResponse(200, {"done": True, "error": {"code": 6, "message": "exists"}}),
    )
    p.add_member("groups/abc", "ada@x")
    p.add_member("groups/abc", "ada@x")
    p.add_member("groups/abc", "ada@x")

    p, _ = _provider(
        FakeResponse(400, {"error": {"message": "Group does not allow members outside the org"}})
    )
    with pytest.raises(ExternalMembersDisabled):
        p.add_member("groups/abc", "ada@gmail.com")

    p, _ = _provider(FakeResponse(403, {"error": {"message": "Permission denied"}}))
    with pytest.raises(GroupProviderError, match="403 Permission denied") as e:
        p.add_member("groups/abc", "ada@x")
    assert not isinstance(e.value, ExternalMembersDisabled)


def test_provider_remove_member_tolerates_already_gone():
    p, s = _provider(
        FakeResponse(200, {"done": True}),
        FakeResponse(404, {}),
        FakeResponse(200, {"done": True, "error": {"code": 5, "message": "gone"}}),
        FakeResponse(500, None),
    )
    p.remove_member("groups/abc/memberships/1")
    p.remove_member("groups/abc/memberships/1")
    p.remove_member("groups/abc/memberships/1")
    assert s.calls[0][0] == "DELETE" and s.calls[0][1].endswith("/groups/abc/memberships/1")
    with pytest.raises(GroupProviderError, match="500"):
        p.remove_member("groups/abc/memberships/1")


@pytest.mark.parametrize(
    "body, match",
    [
        ({"done": False}, "not finished"),
        ({}, "not finished"),
        ({"done": True, "error": {"code": 7, "message": "Permission denied"}}, "Permission denied"),
    ],
)
def test_provider_membership_changes_require_a_finished_clean_operation(body, match):
    p, _ = _provider(FakeResponse(200, body), FakeResponse(200, body))
    with pytest.raises(GroupProviderError, match=match):
        p.add_member("groups/abc", "ada@x")
    with pytest.raises(GroupProviderError, match=match):
        p.remove_member("groups/abc/memberships/1")


def test_unfinished_removal_is_not_reported_as_synced(storage):
    team = _team(storage)
    leaves = _member(storage, team, "leaves@x")
    p, _ = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),  # create group
        FakeResponse(200, {"memberships": []}),
        FakeResponse(200, {"done": True}),  # add leaves@x
    )
    ok = sync_team(storage, p, team.id, actor="t")
    assert ok.status == "synced"

    (m,) = storage.list_memberships(person_id=leaves.id)
    storage.end_membership(m.id, date.today(), actor="t")
    member = {
        "name": "m/1",
        "preferredMemberKey": {"id": "leaves@x"},
        "roles": [{"name": "MEMBER"}],
    }
    p._session.responses = [
        FakeResponse(200, {"memberships": [member]}),
        FakeResponse(200, {"done": False}),  # delete accepted but not finished
    ]
    state = sync_team(storage, p, team.id, actor="t")
    assert state.status == "failed" and "not finished" in state.last_error
    assert state.last_synced_at == ok.last_synced_at


@pytest.mark.parametrize("revoke", ["membership", "person", "team"])
def test_pending_add_survives_retries_and_is_revoked_when_it_finishes(storage, revoke):
    team = _team(storage)
    person = _member(storage, team, "ada@x")
    p, _ = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),
        FakeResponse(200, {}),
        FakeResponse(200, {"name": "operations/add-ada", "done": False}),
    )
    pending = sync_team(storage, p, team.id, actor="t")
    assert pending.status == "failed"
    assert pending.pending_changes[0].operation_name == "operations/add-ada"

    if revoke == "membership":
        (m,) = storage.list_memberships(person_id=person.id)
        storage.end_membership(m.id, date.today(), actor="t")
    elif revoke == "person":
        storage.update_person(person.id, PersonUpdate(active=False), actor="t")
    else:
        storage.update_team(team.id, TeamUpdate(active=False), actor="t")

    # A new provider on each run stands in for a cron process or API restart.
    # Absence must not clear an addition that is still executing remotely.
    for _ in range(2):
        p, session = _provider(FakeResponse(200, {}))
        waiting = sync_team(storage, p, team.id, actor="retry")
        assert waiting.status == "failed" and waiting.pending_changes == pending.pending_changes
        assert waiting.last_synced_at is None
        assert len(session.calls) == 1
        assert synced_team_for_group(storage, waiting.group_email) is None
        assert synced_groups_containing(storage, p, person) == []

    # The old operation completes after the revocation. The next sync observes
    # it, removes the departed person, then permits synced lookups again.
    member = {
        "name": "groups/abc/memberships/ada",
        "preferredMemberKey": {"id": "ada@x"},
        "roles": [{"name": "MEMBER"}],
    }
    p, session = _provider(
        FakeResponse(200, {"memberships": [member]}),
        FakeResponse(200, {"done": True}),
    )
    done = sync_team(storage, p, team.id, actor="retry")
    assert done.status == "synced" and done.pending_changes == []
    assert session.calls[-1][:2] == (
        "DELETE",
        "https://cloudidentity.googleapis.com/v1/groups/abc/memberships/ada",
    )


def test_pending_remove_must_finish_before_member_can_rejoin(storage):
    team = _team(storage)
    person = _member(storage, team, "ada@x")
    p, _ = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),
        FakeResponse(200, {}),
        FakeResponse(200, {"done": True}),
    )
    synced = sync_team(storage, p, team.id, actor="t")
    storage.update_person(person.id, PersonUpdate(active=False), actor="t")
    member = {
        "name": "groups/abc/memberships/ada",
        "preferredMemberKey": {"id": "ada@x"},
        "roles": [{"name": "MEMBER"}],
    }
    p, _ = _provider(
        FakeResponse(200, {"memberships": [member]}),
        FakeResponse(200, {"name": "operations/remove-ada", "done": False}),
    )
    sync_team(storage, p, team.id, actor="t")
    storage.update_person(person.id, PersonUpdate(active=True), actor="t")
    p, session = _provider(FakeResponse(200, {"memberships": [member]}))
    waiting = sync_team(storage, p, team.id, actor="retry")
    assert waiting.status == "failed" and len(waiting.pending_changes) == 1
    assert waiting.last_synced_at == synced.last_synced_at
    assert len(session.calls) == 1

    p, session = _provider(FakeResponse(200, {}), FakeResponse(200, {"done": True}))
    done = sync_team(storage, p, team.id, actor="retry")
    assert done.status == "synced" and done.pending_changes == []
    assert session.calls[-1][0] == "POST"


@pytest.mark.parametrize("settings_failure", [False, True])
def test_pending_add_does_not_block_unrelated_revocations(storage, groups, settings_failure):
    team = _team(storage)
    leaves = _member(storage, team, "leaves@x")
    sync_team(storage, groups, team.id, actor="t")
    _member(storage, team, "pending@x")

    def pending_add(group_name, email):
        raise GroupOperationPending("still running", "operations/add")

    groups.add_member = pending_add
    sync_team(storage, groups, team.id, actor="t")
    storage.update_person(leaves.id, PersonUpdate(active=False), actor="t")
    if settings_failure:
        groups.fail = "settings"
    result = sync_team(storage, groups, team.id, actor="t")
    assert result.status == "failed" and len(result.pending_changes) == 1
    assert result.pending_changes[0].operation_name == "operations/add"
    assert groups.members[result.group_name] == {}


def test_membership_intent_is_durable_before_the_provider_call(storage, groups):
    team = _team(storage)
    person = _member(storage, team, "ada@x")

    def crash_after_sending(group_name, email):
        saved = storage.get_team_google_group(team.id)
        assert saved.status == "failed" and saved.pending_changes[0].email == email
        raise KeyboardInterrupt("process stopped before receiving Google's response")

    groups.add_member = crash_after_sending
    with pytest.raises(KeyboardInterrupt):
        sync_team(storage, groups, team.id, actor="t")
    storage.update_person(person.id, PersonUpdate(active=False), actor="t")
    result = sync_team(storage, groups, team.id, actor="retry")
    assert result.status == "failed" and len(result.pending_changes) == 1


@pytest.mark.parametrize(
    "response", [TimeoutError("token=secret"), FakeResponse(503, {}), FakeResponse(503, [])]
)
def test_uncertain_membership_write_stays_pending(storage, response):
    team = _team(storage)
    person = _member(storage, team, "ada@x")
    p, _ = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),
        FakeResponse(200, {}),
        response,
    )
    result = sync_team(storage, p, team.id, actor="t")
    assert result.status == "failed" and len(result.pending_changes) == 1
    assert "secret" not in result.last_error
    storage.update_person(person.id, PersonUpdate(active=False), actor="t")
    p, _ = _provider(FakeResponse(200, {}))
    assert sync_team(storage, p, team.id, actor="retry").status == "failed"


def test_provider_transport_failure_hides_details():
    p, _ = _provider(ConnectionError("token=secret-value"))
    with pytest.raises(GroupProviderError) as e:
        p.list_members("groups/abc")
    assert "secret-value" not in str(e.value)


@pytest.mark.parametrize("exception_type", ["RefreshError", "TransportError"])
def test_refresh_failure_rejects_the_write_without_leaving_pending_intent(storage, exception_type):
    from google.auth import exceptions

    team = _team(storage)
    _member(storage, team, "ada@x")
    p, _ = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),
        FakeResponse(200, {}),
        getattr(exceptions, exception_type)("secret refresh token"),
    )
    result = sync_team(storage, p, team.id, actor="t")
    assert result.status == "failed" and result.pending_changes == []
    assert "secret" not in result.last_error

    p, _ = _provider(FakeResponse(200, {}), FakeResponse(200, {"done": True}))
    assert sync_team(storage, p, team.id, actor="retry").status == "synced"


@pytest.mark.parametrize(
    "initial_response",
    [FakeResponse(200, {"name": "operations/remove-ada", "done": False}), FakeResponse(503, {})],
)
def test_lost_removal_is_retried_without_losing_the_original_pending_change(
    storage, initial_response
):
    team = _team(storage)
    person = _member(storage, team, "ada@x")
    p, _ = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),
        FakeResponse(200, {}),
        FakeResponse(200, {"done": True}),
    )
    sync_team(storage, p, team.id, actor="t")
    storage.update_person(person.id, PersonUpdate(active=False), actor="t")
    member = {
        "name": "groups/abc/memberships/ada",
        "preferredMemberKey": {"id": "ada@x"},
        "roles": [{"name": "MEMBER"}],
    }
    p, _ = _provider(
        FakeResponse(200, {"memberships": [member]}),
        initial_response,
    )
    pending = sync_team(storage, p, team.id, actor="t")
    for response in (
        FakeResponse(503, {}),
        FakeResponse(403, {"error": {"message": "Permission denied"}}),
    ):
        p, session = _provider(FakeResponse(200, {"memberships": [member]}), response)
        failed = sync_team(storage, p, team.id, actor="retry")
        assert failed.status == "failed" and failed.pending_changes == pending.pending_changes
        assert session.calls[-1][0] == "DELETE"

    p, session = _provider(
        FakeResponse(200, {"memberships": [member]}), FakeResponse(200, {"done": True})
    )
    result = sync_team(storage, p, team.id, actor="retry")
    assert result.status == "synced" and result.pending_changes == []
    assert session.calls[-1][0] == "DELETE"


def test_pending_add_is_not_replayed_even_while_the_person_is_still_wanted(storage):
    team = _team(storage)
    _member(storage, team, "ada@x")
    p, _ = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),
        FakeResponse(200, {}),
        FakeResponse(200, {"name": "operations/add-ada", "done": False}),
    )
    pending = sync_team(storage, p, team.id, actor="t")
    p, session = _provider(FakeResponse(200, {}))
    failed = sync_team(storage, p, team.id, actor="retry")
    assert failed.status == "failed" and failed.pending_changes == pending.pending_changes
    assert [call[0] for call in session.calls] == ["GET"]


def test_connect_timeout_can_retry_without_operator_recovery(storage):
    from requests.exceptions import ConnectTimeout

    team = _team(storage)
    _member(storage, team, "ada@x")
    p, _ = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),
        FakeResponse(200, {}),
        ConnectTimeout("connection never established"),
    )
    failed = sync_team(storage, p, team.id, actor="t")
    assert failed.status == "failed" and failed.pending_changes == []

    p, session = _provider(FakeResponse(200, {}), FakeResponse(200, {"done": True}))
    result = sync_team(storage, p, team.id, actor="retry")
    assert result.status == "synced"
    assert [call[0] for call in session.calls] == ["GET", "POST"]


@pytest.mark.parametrize("body", [None, [], "invalid"])
def test_invalid_google_read_cannot_clear_pending_removal(storage, body):
    team = _team(storage)
    person = _member(storage, team, "ada@x")
    p, _ = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),
        FakeResponse(200, {}),
        FakeResponse(200, {"done": True}),
    )
    synced = sync_team(storage, p, team.id, actor="t")
    storage.update_person(person.id, PersonUpdate(active=False), actor="t")
    member = {
        "name": "groups/abc/memberships/ada",
        "preferredMemberKey": {"id": "ada@x"},
        "roles": [{"name": "MEMBER"}],
    }
    p, _ = _provider(
        FakeResponse(200, {"memberships": [member]}),
        FakeResponse(200, {"name": "operations/remove-ada", "done": False}),
    )
    pending = sync_team(storage, p, team.id, actor="t")
    p, _ = _provider(FakeResponse(200, body))
    failed = sync_team(storage, p, team.id, actor="retry")
    assert failed.status == "failed" and failed.pending_changes == pending.pending_changes
    assert failed.last_synced_at == synced.last_synced_at
    assert synced_team_for_group(storage, failed.group_email) is None


# --- Config ---


def test_partial_google_config_refuses_to_boot():
    with pytest.raises(RuntimeError, match="GOOGLE_OAUTH_REFRESH_TOKEN"):
        verify_production_secrets(
            Settings(
                google_groups_customer_id="C01",
                google_groups_domain="utmist.example",
                google_oauth_client_id="id",
                google_oauth_client_secret="secret",
            )
        )
    verify_production_secrets(Settings())


# --- Automatic external-member settings ---


def _settings_body(**changes):
    return {
        "email": "ml@utmist.example",
        "allowExternalMembers": "true",
        "whoCanAddExternalMembers": "END_USERS_CAN_ADD_EXTERNAL_MEMBERS",
        **changes,
    }


def _settings_provider(*responses):
    provider, membership_session = _provider()
    settings_session = FakeSession(*responses)
    provider._settings_session = settings_session
    return provider, settings_session, membership_session


def test_settings_patch_is_minimal_verified_and_idempotent():
    before = _settings_body(
        allowExternalMembers="false",
        whoCanAddExternalMembers="ONLY_ADMINS_CAN_ADD_EXTERNAL_MEMBERS",
        whoCanViewGroup="ALL_MEMBERS_CAN_VIEW",
    )
    after = _settings_body(whoCanViewGroup="ALL_MEMBERS_CAN_VIEW")
    p, session, members = _settings_provider(
        FakeResponse(200, before),
        FakeResponse(200, after),
        FakeResponse(200, after),
        FakeResponse(200, after),
    )
    p.ensure_external_members("ml@utmist.example")
    p.ensure_external_members("ml@utmist.example")
    assert [c[0] for c in session.calls] == ["GET", "PATCH", "GET", "GET"]
    assert session.calls[1][2]["json"] == {
        "allowExternalMembers": "true",
        "whoCanAddExternalMembers": "END_USERS_CAN_ADD_EXTERNAL_MEMBERS",
    }
    assert all(c[1].endswith("/groups/ml%40utmist.example") for c in session.calls)
    assert all(c[2]["allow_redirects"] is False for c in session.calls)
    assert all(c[2]["params"] == {"alt": "json"} for c in session.calls)
    assert members.calls == []


def test_settings_patch_only_changes_the_incorrect_setting():
    p, session, _ = _settings_provider(
        FakeResponse(200, _settings_body(allowExternalMembers="false")),
        FakeResponse(200, _settings_body()),
        FakeResponse(200, _settings_body()),
    )
    p.ensure_external_members("ml@utmist.example")
    assert session.calls[1][2]["json"] == {"allowExternalMembers": "true"}


def test_fresh_disabled_group_can_omit_the_dependent_external_add_setting():
    before = {"email": "ml@utmist.example", "allowExternalMembers": "false"}
    p, session, _ = _settings_provider(
        FakeResponse(200, before),
        FakeResponse(200, _settings_body()),
        FakeResponse(200, _settings_body()),
    )
    p.ensure_external_members("ml@utmist.example")
    assert session.calls[1][2]["json"] == {
        "allowExternalMembers": "true",
        "whoCanAddExternalMembers": "END_USERS_CAN_ADD_EXTERNAL_MEMBERS",
    }
    assert [c[0] for c in session.calls] == ["GET", "PATCH", "GET"]


def test_successful_patch_requires_readback_not_just_its_response():
    before = _settings_body(allowExternalMembers="false")
    p, _, _ = _settings_provider(
        FakeResponse(200, before),
        FakeResponse(200, _settings_body()),
        FakeResponse(200, before),
    )
    with pytest.raises(GroupProviderError, match="not confirmed"):
        p.ensure_external_members("ml@utmist.example")


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"email": None},
        _settings_body(email="other@x"),
        _settings_body(allowExternalMembers=True),
        _settings_body(whoCanAddExternalMembers=None),
    ],
)
def test_malformed_settings_reads_never_trigger_a_patch(body):
    p, session, _ = _settings_provider(FakeResponse(200, body))
    with pytest.raises(GroupProviderError, match="group settings"):
        p.ensure_external_members("ml@utmist.example")
    assert [c[0] for c in session.calls] == ["GET"]


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 503])
def test_settings_http_errors_are_sanitized_and_do_not_retry(status):
    p, session, _ = _settings_provider(FakeResponse(status, {"error": "secret-token"}))
    with pytest.raises(GroupProviderError, match=f"HTTP {status}") as e:
        p.ensure_external_members("ml@utmist.example")
    assert "secret-token" not in str(e.value)
    assert len(session.calls) == 1


def test_lost_settings_patch_response_is_recovered_by_reading_before_retry():
    p, session, _ = _settings_provider(
        FakeResponse(200, _settings_body(allowExternalMembers="false")),
        TimeoutError("secret-token"),
        FakeResponse(200, _settings_body()),
    )
    with pytest.raises(GroupProviderError, match="TimeoutError") as e:
        p.ensure_external_members("ml@utmist.example")
    assert "secret-token" not in str(e.value)
    assert not isinstance(e.value, GroupOperationPending)
    p.ensure_external_members("ml@utmist.example")
    assert [c[0] for c in session.calls] == ["GET", "PATCH", "GET"]


def test_settings_use_independent_oauth_scopes(monkeypatch):
    from google.auth.transport import requests
    from src.providers.google_groups import SCOPE, SETTINGS_SCOPE

    credentials = []

    def session(creds):
        credentials.append(creds)
        return FakeSession()

    monkeypatch.setattr(requests, "AuthorizedSession", session)
    p = GoogleGroupsProvider(
        customer_id="C01",
        domain="utmist.example",
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
    )
    assert [c.scopes for c in credentials] == [[SCOPE], [SETTINGS_SCOPE]]
    assert p._session is not p._settings_session


def test_settings_failure_blocks_additions_but_preserves_revocations_and_recovery(storage, groups):
    team = _team(storage)
    leaves = _member(storage, team, "leaves@gmail.com")
    first = sync_team(storage, groups, team.id, actor="t")
    storage.update_person(leaves.id, PersonUpdate(active=False), actor="t")
    _member(storage, team, "joins@gmail.com")
    groups.fail = "settings"
    failed = sync_team(storage, groups, team.id, actor="t")
    assert failed.status == "failed" and "settings" in failed.last_error
    assert failed.last_synced_at == first.last_synced_at
    assert _emails(groups, failed) == set()
    assert failed.pending_changes == []
    assert synced_team_for_group(storage, failed.group_email) is None
    groups.fail = None
    recovered = sync_team(storage, groups, team.id, actor="t")
    assert recovered.status == "synced" and recovered.group_name == first.group_name
    assert _emails(groups, recovered) == {"joins@gmail.com"}
    assert len(groups.groups) == 1


def test_empty_active_group_requires_settings_but_retirement_does_not(storage, groups):
    team = _team(storage)
    groups.fail = "settings"
    failed = sync_team(storage, groups, team.id, actor="t")
    assert failed.status == "failed" and failed.group_name
    assert failed.last_synced_at is None
    groups.settings_calls.clear()
    storage.update_team(team.id, TeamUpdate(active=False), actor="t")
    retired = sync_team(storage, groups, team.id, actor="t")
    assert retired.status == "synced"
    assert groups.settings_calls == []


def test_settings_retry_uses_persisted_group_address_after_team_rename(storage, groups):
    team = _team(storage)
    groups.fail = "settings"
    first = sync_team(storage, groups, team.id, actor="t")
    storage.update_team(team.id, TeamUpdate(slug="renamed"), actor="t")
    groups.fail = None
    state = sync_team(storage, groups, team.id, actor="t")
    assert state.status == "synced" and state.group_email == first.group_email
    assert groups.settings_calls == [first.group_email, first.group_email]
    assert len(groups.groups) == 1


def test_settings_crash_leaves_confirmed_group_identity_and_invalidates_synced_state(
    storage, groups
):
    team = _team(storage)
    first = sync_team(storage, groups, team.id, actor="t")

    def crash(email):
        saved = storage.get_team_google_group(team.id)
        assert saved.group_name == first.group_name and saved.status == "failed"
        raise KeyboardInterrupt()

    groups.ensure_external_members = crash
    with pytest.raises(KeyboardInterrupt):
        sync_team(storage, groups, team.id, actor="t")
    assert synced_team_for_group(storage, first.group_email) is None


def test_fresh_group_runs_real_settings_provider_before_external_add(storage):
    team = _team(storage)
    _member(storage, team, "ada@gmail.com")
    p, members = _provider(
        FakeResponse(200, {"done": True, "response": {"name": "groups/abc"}}),
        FakeResponse(200, {"memberships": []}),
        FakeResponse(200, {"done": True}),
    )
    settings = FakeSession(
        FakeResponse(200, _settings_body(allowExternalMembers="false")),
        FakeResponse(200, _settings_body()),
        FakeResponse(200, _settings_body()),
    )
    p._settings_session = settings
    original = members.request

    def request(method, url, **kwargs):
        if method == "POST" and url.endswith("/memberships"):
            assert [c[0] for c in settings.calls] == ["GET", "PATCH", "GET"]
        return original(method, url, **kwargs)

    members.request = request
    state = sync_team(storage, p, team.id, actor="integration")
    assert state.status == "synced" and state.updated_by == "integration"
    assert state.pending_changes == []


def test_settings_refresh_failure_does_not_prevent_google_membership_removal(storage, groups):
    from google.auth.exceptions import RefreshError

    team = _team(storage)
    person = _member(storage, team, "leaves@gmail.com")
    first = sync_team(storage, groups, team.id, actor="t")
    storage.update_person(person.id, PersonUpdate(active=False), actor="t")
    _member(storage, team, "joins@gmail.com")
    member_name = groups.members[first.group_name][person.primary_email]
    provider, members = _provider(
        FakeResponse(
            200,
            {
                "memberships": [
                    {
                        "name": member_name,
                        "preferredMemberKey": {"id": person.primary_email},
                        "roles": [{"name": "MEMBER"}],
                    }
                ]
            },
        ),
        FakeResponse(200, {"done": True}),
    )
    provider._settings_session = FakeSession(RefreshError("secret-token"))
    state = sync_team(storage, provider, team.id, actor="retry")
    assert state.status == "failed" and "RefreshError" in state.last_error
    assert "secret-token" not in state.last_error
    assert state.last_synced_at == first.last_synced_at
    assert [call[0] for call in members.calls] == ["GET", "DELETE"]
    assert members.calls[-1][1].endswith(member_name)
    assert state.pending_changes == []
