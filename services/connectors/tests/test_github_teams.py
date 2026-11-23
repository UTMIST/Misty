from uuid import uuid4

import httpx
import pytest

from contracts.github_teams import GitHubTeamError
from src.sources.github_teams import GitHubTeamsProvider


def test_uncertain_provision_reconciles_without_second_post():
    tid = uuid4()
    posted = []
    team = {
        "id": 77,
        "organization": {"id": 123},
        "slug": f"misty-{tid.hex}",
        "name": "Managed",
        "description": f"Misty managed team {tid}",
        "parent": None,
    }

    def handle(request):
        if request.method == "POST":
            posted.append(True)
            raise httpx.ReadTimeout("secret response", request=request)
        if request.url.path == "/orgs/UTMIST":
            return httpx.Response(200, json={"id": 123})
        if request.url.path.endswith("/team-sync/group-mappings"):
            return httpx.Response(404)
        if request.url.path.endswith("/teams"):
            return httpx.Response(200, json=[])
        if not posted:
            return httpx.Response(404)
        return httpx.Response(200, json=team)

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handle))
    provider = GitHubTeamsProvider(
        token="sensitive", organization_id=123, organization="UTMIST", client=client
    )
    with pytest.raises(GitHubTeamError) as e:
        provider.provision(tid, reconcile_only=False)
    assert e.value.uncertain and "secret" not in str(e.value)
    assert provider.provision(tid, reconcile_only=True).team_id == 77
    assert len(posted) == 1


def test_wrong_org_and_nested_rejected():
    def handle(request):
        if request.url.path == "/orgs/UTMIST":
            return httpx.Response(200, json={"id": 123})
        return httpx.Response(200, json={"id": 77, "organization": {"id": 999}})

    p = GitHubTeamsProvider(
        token="secret",
        organization_id=123,
        organization="UTMIST",
        client=httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handle)
        ),
    )
    with pytest.raises(GitHubTeamError, match="organization_mismatch"):
        p.inspect(77)


@pytest.mark.parametrize("unsupported", ["nested", "idp"])
def test_unsupported_managed_team_is_rejected(unsupported):
    def handle(request):
        path = request.url.path
        if path == "/orgs/UTMIST":
            return httpx.Response(200, json={"id": 123})
        if path.endswith("/team-sync/group-mappings"):
            return httpx.Response(200, json={"groups": [{"group_id": "external"}]})
        if path.endswith("/teams"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "id": 77,
                "organization": {"id": 123},
                "slug": "flat",
                "name": "Flat",
                "parent": {"id": 1} if unsupported == "nested" else None,
            },
        )

    provider = GitHubTeamsProvider(
        token="secret",
        organization_id=123,
        organization="UTMIST",
        client=httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handle)
        ),
    )
    with pytest.raises(
        GitHubTeamError,
        match="nested_team_unsupported"
        if unsupported == "nested"
        else "idp_managed_team_unsupported",
    ):
        provider.inspect(77)


@pytest.mark.parametrize("failed_path", ["/orgs/UTMIST", "/orgs/UTMIST/teams/live/teams"])
def test_auxiliary_404_does_not_report_team_deleted(failed_path):
    def handle(request):
        if request.url.path == failed_path:
            return httpx.Response(404)
        if request.url.path == "/orgs/UTMIST":
            return httpx.Response(200, json={"id": 123})
        return httpx.Response(
            200, json={"id": 77, "organization": {"id": 123}, "slug": "live", "name": "Live"}
        )

    provider = GitHubTeamsProvider(
        token="fake",
        organization_id=123,
        organization="UTMIST",
        client=httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handle)
        ),
    )
    with pytest.raises(GitHubTeamError, match="inspection_unavailable"):
        provider.inspect(77)


@pytest.mark.parametrize("failure", ["org", "lookup", "rejected", "timeout", "invalid", "server"])
def test_provision_retryability_tracks_whether_a_create_might_have_succeeded(failure):
    posts = []

    def handle(request):
        if request.url.path == "/orgs/UTMIST":
            return (
                httpx.Response(503) if failure == "org" else httpx.Response(200, json={"id": 123})
            )
        if request.method == "GET":
            return httpx.Response(503 if failure == "lookup" else 404)
        posts.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("ambiguous", request=request)
        if failure == "invalid":
            return httpx.Response(201, text="invalid JSON")
        return httpx.Response(503 if failure == "server" else 403)

    provider = GitHubTeamsProvider(
        token="fake",
        organization_id=123,
        organization="UTMIST",
        client=httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handle)
        ),
    )
    with pytest.raises(GitHubTeamError) as error:
        provider.provision(uuid4(), reconcile_only=False)
    assert error.value.retryable is (failure in ("org", "lookup", "rejected"))
    assert error.value.uncertain is (failure in ("timeout", "invalid", "server"))
    assert bool(posts) is (failure not in ("org", "lookup"))
