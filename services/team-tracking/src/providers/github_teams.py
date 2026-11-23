from uuid import UUID

import httpx

from contracts.github import GitHubProviderError


class HttpGitHubTeamsClient:
    def __init__(self, *, url: str, key: str, organization_id: int, client=None):
        self.organization_id = organization_id
        self.url = url.rstrip("/")
        self.key = key
        self.client = client or httpx.Client(timeout=20)

    def _call(self, method, path, **kwargs):
        if not self.url or not self.key or not self.organization_id:
            raise GitHubProviderError("not_configured")
        try:
            r = self.client.request(
                method, self.url + path, headers={"X-API-Key": self.key}, **kwargs
            )
            if r.status_code == 404:
                raise GitHubProviderError("not_found")
            if r.status_code == 409:
                detail = r.json().get("detail", {})
                code = detail.get("code") if isinstance(detail, dict) else None
                if code in (
                    "organization_mismatch",
                    "nested_team_unsupported",
                    "idp_managed_team_unsupported",
                    "name_conflict",
                ):
                    raise GitHubProviderError(code)
            if r.status_code >= 400:
                raise GitHubProviderError("provider_unavailable")
            data = r.json()
            if data["organization_id"] != self.organization_id or not isinstance(
                data["team_id"], int
            ):
                raise GitHubProviderError("organization_mismatch")
            return data
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise GitHubProviderError("provider_unavailable") from None

    def inspect(self, team_id: int):
        return self._call("GET", f"/github/teams/{team_id}")

    def provision(self, team_id: UUID, *, reconcile_only: bool):
        return self._call(
            "POST",
            "/github/teams",
            json={"misty_team_id": str(team_id), "reconcile_only": reconcile_only},
        )
