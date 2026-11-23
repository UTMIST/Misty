"""Bounded administrative GitHub calls. Never imports FastAPI or logs payloads."""

from uuid import UUID

import httpx

from contracts.github_teams import GitHubTeam, GitHubTeamError


class GitHubTeamsProvider:
    def __init__(
        self,
        *,
        token: str,
        organization_id: int,
        organization: str,
        timeout: float = 15,
        client: httpx.Client | None = None,
    ):
        self.token = token
        self.organization_id = organization_id
        self.organization = organization
        self.client = client or httpx.Client(base_url="https://api.github.com", timeout=timeout)

    def _request(self, method, path, **kwargs):
        if not self.token or not self.organization_id:
            raise GitHubTeamError("not_configured")
        try:
            with self.client.stream(
                method,
                path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                **kwargs,
            ) as r:
                if r.status_code == 404:
                    raise GitHubTeamError("not_found")
                if r.status_code in (401, 403):
                    raise GitHubTeamError("provider_forbidden")
                if r.status_code >= 400:
                    raise GitHubTeamError(
                        "provider_unavailable", uncertain=method != "GET" and r.status_code >= 500
                    )
                content = bytearray()
                for chunk in r.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise GitHubTeamError(
                            "invalid_provider_response", uncertain=method != "GET"
                        )
                import json

                return json.loads(content)
        except httpx.HTTPError:
            raise GitHubTeamError("provider_unavailable", uncertain=method != "GET") from None
        except ValueError:
            raise GitHubTeamError("invalid_provider_response", uncertain=method != "GET") from None

    def _org(self):
        data = self._request("GET", f"/orgs/{self.organization}")
        if not isinstance(data, dict) or data.get("id") != self.organization_id:
            raise GitHubTeamError("organization_mismatch")

    def _model(self, data):
        try:
            if data["organization"]["id"] != self.organization_id:
                raise GitHubTeamError("organization_mismatch")
            if data.get("parent"):
                raise GitHubTeamError("nested_team_unsupported")
            return GitHubTeam(
                organization_id=self.organization_id,
                team_id=data["id"],
                name=data["name"],
                slug=data["slug"],
                managed_marker=data.get("description"),
            )
        except (KeyError, TypeError, ValueError):
            raise GitHubTeamError("invalid_provider_response") from None

    def inspect(self, team_id: int) -> GitHubTeam:
        try:
            self._org()
        except GitHubTeamError as e:
            if e.code == "not_found":
                raise GitHubTeamError("inspection_unavailable") from None
            raise
        model = self._model(self._request("GET", f"/teams/{team_id}"))
        try:
            children = self._request(
                "GET", f"/orgs/{self.organization}/teams/{model.slug}/teams", params={"per_page": 1}
            )
        except GitHubTeamError as e:
            if e.code == "not_found":
                raise GitHubTeamError("inspection_unavailable") from None
            raise
        if children:
            raise GitHubTeamError("nested_team_unsupported")
        # Enterprise team-sync returns current IdP mappings. A 404 can also mean
        # the feature is unavailable; explicit owner review remains mandatory.
        try:
            sync = self._request(
                "GET", f"/orgs/{self.organization}/teams/{model.slug}/team-sync/group-mappings"
            )
        except GitHubTeamError as e:
            if e.code != "not_found":
                raise
        else:
            if not isinstance(sync, dict) or not isinstance(sync.get("groups"), list):
                raise GitHubTeamError("invalid_provider_response")
            if sync["groups"]:
                raise GitHubTeamError("idp_managed_team_unsupported")
        return model

    def provision(self, team_id: UUID, *, reconcile_only: bool) -> GitHubTeam:
        try:
            self._org()
        except GitHubTeamError as e:
            raise GitHubTeamError(e.code, retryable=True) from None
        # Stable generated slug + description bind recovery to the exact Misty UUID.
        slug = f"misty-{team_id.hex}"
        marker = f"Misty managed team {team_id}"
        try:
            existing = self._request("GET", f"/orgs/{self.organization}/teams/{slug}")
        except GitHubTeamError as e:
            if e.code != "not_found":
                raise GitHubTeamError(e.code, retryable=True) from None
        else:
            if existing.get("description") != marker:
                raise GitHubTeamError("name_conflict")
            return self.inspect(existing["id"])
        if reconcile_only:
            raise GitHubTeamError("creation_unresolved", uncertain=True)
        try:
            data = self._request(
                "POST",
                f"/orgs/{self.organization}/teams",
                json={"name": slug, "description": marker, "privacy": "closed", "repo_names": []},
            )
        except GitHubTeamError as e:
            raise GitHubTeamError(
                e.code, uncertain=e.uncertain, retryable=not e.uncertain
            ) from None
        try:
            return self.inspect(data["id"])
        except (KeyError, GitHubTeamError):
            raise GitHubTeamError("creation_unresolved", uncertain=True) from None
