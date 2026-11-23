import httpx
import pytest

from contracts.github_oauth import GitHubOAuthError
from src.sources.github_oauth import GitHubOAuth


def test_exchange_pkce_expiring_tokens_and_token_revocation():
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 77, "login": "verified"})
        assert b"code_verifier=private-verifier" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 100,
                "refresh_token_expires_in": 200,
            },
        )

    provider = GitHubOAuth(
        client_id="id",
        client_secret="secret",
        callback_url="https://callback.example/github/oauth/callback",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        clock=lambda: 1000,
    )
    result = provider.exchange("private-code", "private-verifier")
    assert result["expires_at"] == 1100
    assert provider.profile(result["access_token"])["github_id"] == 77
    provider.revoke(result["access_token"])
    assert requests[-1].method == "DELETE"


def test_vendor_error_never_reaches_exception_text():
    p = GitHubOAuth(
        client_id="id",
        client_secret="secret",
        callback_url="https://callback.example/github/oauth/callback",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(500, text="SECRET credential details")
            )
        ),
    )
    with pytest.raises(GitHubOAuthError) as error:
        p.exchange("code", "verifier")
    assert "SECRET" not in str(error.value)
