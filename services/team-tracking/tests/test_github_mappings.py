import pytest
from fastapi.testclient import TestClient

from contracts.github import GitHubProviderError
from contracts.types import TeamCreate, TeamUpdate
from src.api.app import create_app
from src.api.deps import get_github_teams, get_storage
from src.config import get_settings
from src.storage.in_memory import InMemoryStorageAdapter


class Provider:
    organization_id = 123
    calls = 0
    unavailable = False
    name = "renamed"

    def inspect(self, team_id):
        self.calls += 1
        if self.unavailable:
            raise GitHubProviderError("not_found")
        return dict(organization_id=123, team_id=team_id, slug=self.name, name=self.name)

    def provision(self, team_id, *, reconcile_only):
        self.calls += 1
        if not reconcile_only:
            raise GitHubProviderError("provider_unavailable")
        return dict(organization_id=123, team_id=77, slug="created", name="created")


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    get_settings.cache_clear()
    store = InMemoryStorageAdapter()
    provider = Provider()
    team = store.create_team(TeamCreate(slug="ops", label="Ops"), actor="seed")
    app = create_app()
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[get_github_teams] = lambda: provider
    with TestClient(app) as client:
        yield client, store, provider, team


def test_explicit_adoption_rename_retirement_and_no_reactivation(setup):
    client, store, provider, team = setup
    path = f"/teams/{team.id}/github-team"
    assert client.post(path, json={"mode": "adopt", "github_team_id": 77}).status_code == 401
    headers = {"X-API-Key": "test-key"}
    assert (
        client.post(path, headers=headers, json={"mode": "adopt", "github_team_id": 77}).status_code
        == 422
    )
    assert provider.calls == 0
    result = client.post(
        path,
        headers=headers,
        json={"mode": "adopt", "github_team_id": 77, "adoption_reviewed": True},
    )
    assert result.status_code == 200
    assert result.json()["status"] == "active"
    provider.name = "new-name"
    assert client.post(path + "/reconcile", headers=headers).json()["slug"] == "new-name"
    other = store.create_team(TeamCreate(slug="other", label="Other"), actor="seed")
    assert (
        client.post(
            f"/teams/{other.id}/github-team",
            headers=headers,
            json={"mode": "adopt", "github_team_id": 77, "adoption_reviewed": True},
        ).status_code
        == 409
    )
    store.update_team(team.id, TeamUpdate(active=False), actor="admin")
    assert client.get(path, headers=headers).json()["status"] == "inactive"
    store.update_team(team.id, TeamUpdate(active=True), actor="admin")
    assert client.get(path, headers=headers).json()["status"] == "inactive"


def test_uncertain_create_cannot_be_retried_but_can_reconcile(setup):
    client, store, provider, team = setup
    path = f"/teams/{team.id}/github-team"
    headers = {"X-API-Key": "test-key"}
    assert client.post(path, headers=headers, json={"mode": "provision"}).status_code == 502
    assert client.get(path, headers=headers).json()["status"] == "pending"
    assert client.post(path, headers=headers, json={"mode": "provision"}).status_code == 409
    assert provider.calls == 1
    assert client.post(path + "/reconcile", headers=headers).json()["github_team_id"] == 77
    provider.unavailable = True
    assert client.post(path + "/reconcile", headers=headers).status_code == 502
    assert client.get(path, headers=headers).json()["status"] == "inactive"


def test_mapping_contract():
    from github_mapping_contract import check_github_mapping_contract

    check_github_mapping_contract(InMemoryStorageAdapter())


def test_plain_team_write_scope_cannot_manage_github(setup):
    from src.api.hashing import generate_key

    client, store, provider, team = setup
    token, prefix, hashed = generate_key()
    store.create_api_key(
        name="ordinary", prefix=prefix, key_hash=hashed, scopes=["teams:write"], actor="admin"
    )
    response = client.post(
        f"/teams/{team.id}/github-team", headers={"X-API-Key": token}, json={"mode": "provision"}
    )
    assert response.status_code == 403
    assert store.get_github_mapping(team.id) is None and provider.calls == 0


def test_definitive_prewrite_failure_can_retry(setup):
    client, store, provider, team = setup
    path = f"/teams/{team.id}/github-team"
    headers = {"X-API-Key": "test-key"}

    def rejected(*args, **kwargs):
        raise GitHubProviderError("provider_unavailable", retryable=True)

    provider.provision = rejected
    assert client.post(path, headers=headers, json={"mode": "provision"}).status_code == 502
    assert store.get_github_mapping(team.id).status == "retryable"
    provider.provision = lambda *args, **kwargs: dict(
        organization_id=123, team_id=77, slug="created", name="Created"
    )
    response = client.post(path, headers=headers, json={"mode": "provision"})
    assert response.status_code == 200 and response.json()["status"] == "active"


@pytest.mark.parametrize("operation", ["read", "reconcile"])
def test_auxiliary_lookup_failure_keeps_mapping_active(setup, operation):
    client, store, provider, team = setup
    store.reserve_github_mapping(team.id, 123, actor="admin", github_team_id=77)
    store.finish_github_mapping(team.id, 123, 77, "old", "Old", actor="admin")

    def unavailable(*args):
        raise GitHubProviderError("inspection_unavailable")

    provider.inspect = unavailable
    path = f"/teams/{team.id}/github-team"
    headers = {"X-API-Key": "test-key"}
    response = (
        client.get(path, headers=headers)
        if operation == "read"
        else client.post(path + "/reconcile", headers=headers)
    )
    assert response.status_code == 502
    assert store.get_github_mapping(team.id).status == "active"
