from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import build_seed_providers, build_seed_role_kinds
from contracts.groups import ExternalMembersDisabled, GroupProviderError
from contracts.types import PersonCreate, PersonUpdate, TeamCreate, TeamMembershipCreate, TeamUpdate
from src.api.app import create_app
from src.api.deps import get_group_provider, get_storage
from src.config import Settings, verify_production_secrets
from src.google_groups import sync_team
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


def test_sync_endpoint(client, groups):
    groups.fail = "create_group"
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


def _provider(*responses):
    session = FakeSession(*responses)
    p = GoogleGroupsProvider(
        customer_id="C01",
        domain="utmist.example",
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        session=session,
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
    p, _ = _provider(FakeResponse(200, {"done": True}), FakeResponse(409, {}))
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
    p, s = _provider(FakeResponse(200, {}), FakeResponse(404, {}), FakeResponse(500, None))
    p.remove_member("groups/abc/memberships/1")
    p.remove_member("groups/abc/memberships/1")
    assert s.calls[0][0] == "DELETE" and s.calls[0][1].endswith("/groups/abc/memberships/1")
    with pytest.raises(GroupProviderError, match="500"):
        p.remove_member("groups/abc/memberships/1")


def test_provider_transport_failure_hides_details():
    p, _ = _provider(ConnectionError("token=secret-value"))
    with pytest.raises(GroupProviderError) as e:
        p.list_members("groups/abc")
    assert "secret-value" not in str(e.value)


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
