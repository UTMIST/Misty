import httpx

from contracts.github_oauth import GitHubOAuthError


class HttpGitHubDirectory:
    def __init__(self, url, key, client=None):
        self.url, self.key = url.rstrip("/"), key
        self.client = client or httpx.Client(timeout=10)

    def _call(self, method, path, **kwargs):
        try:
            r = self.client.request(
                method, self.url + path, headers={"X-API-Key": self.key}, **kwargs
            )
            if r.status_code >= 400:
                raise GitHubOAuthError("directory_binding_rejected")
            return r.json() if r.content else None
        except (httpx.HTTPError, ValueError):
            raise GitHubOAuthError("directory_unavailable") from None

    def validate(self, person_id, discord_id):
        data = self._call("GET", f"/people/by-identifier/discord/{discord_id}")
        if not data or data.get("id") != person_id or data.get("active") is not True:
            raise GitHubOAuthError("directory_binding_rejected")

    def verify(self, person_id, discord_id, github_id, login):
        self._call(
            "PUT",
            f"/people/{person_id}/github",
            json=dict(discord_id=discord_id, github_id=github_id, login=login),
        )

    def unlink(self, person_id):
        self._call("DELETE", f"/people/{person_id}/github")
