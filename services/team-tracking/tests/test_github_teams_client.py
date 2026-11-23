from uuid import uuid4

import httpx
import pytest

from contracts.github import GitHubProviderError
from src.providers.github_teams import HttpGitHubTeamsClient


@pytest.mark.parametrize(
    "detail,retryable",
    [
        ({"code": "provider_unavailable", "uncertain": False, "retryable": True}, True),
        ({"code": "provider_unavailable", "uncertain": True, "retryable": True}, False),
        ({"code": "provider_unavailable", "uncertain": False}, False),
        ("invalid", False),
    ],
)
def test_only_explicit_definitive_errors_allow_retry(detail, retryable):
    client = HttpGitHubTeamsClient(
        url="https://connector.example",
        key="fake",
        organization_id=123,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(502, json={"detail": detail})
            )
        ),
    )
    with pytest.raises(GitHubProviderError) as error:
        client.provision(uuid4(), reconcile_only=False)
    assert error.value.retryable is retryable


def test_lost_connector_response_cannot_allow_retry():
    def handle(request):
        raise httpx.ReadTimeout("ambiguous", request=request)

    client = HttpGitHubTeamsClient(
        url="https://connector.example",
        key="fake",
        organization_id=123,
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(GitHubProviderError) as error:
        client.provision(uuid4(), reconcile_only=False)
    assert not error.value.retryable
