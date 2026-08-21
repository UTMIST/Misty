from urllib.parse import quote

import httpx

from contracts.directory import DirectoryUnavailable

_TIMEOUT = httpx.Timeout(5.0)


class HttpDirectoryClient:
    """Looks up people and identifiers over team-tracking's HTTP API. A 404
    means 'no such record' (returns None); connection failure or 5xx means
    'directory unavailable' (raises DirectoryUnavailable).

    Holds one pooled httpx.Client for its whole lifetime. The resolver makes two
    directory calls per request, so a client built (and closed) per call would
    mean two fresh TCP + TLS handshakes on the hot path. Construct this once —
    src/api/deps.py caches the instance — rather than per request.
    """

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        # Set once as a default header so the key never has to be rebuilt (or
        # accidentally logged) per call. It is never read back out.
        self._client = client or httpx.Client(timeout=_TIMEOUT)
        self._client.headers["X-API-Key"] = api_key

    def _get(self, path: str):
        try:
            resp = self._client.get(f"{self._base_url}{path}")
        except httpx.HTTPError as e:
            raise DirectoryUnavailable(f"directory unreachable: {e}") from e
        if resp.status_code == 404:
            return None
        if not (200 <= resp.status_code < 300):
            raise DirectoryUnavailable(f"directory returned {resp.status_code}")
        return resp.json()

    def get_person_by_github(self, github_login: str) -> dict | None:
        return self._get(f"/people/by-identifier/github/{quote(github_login, safe='')}")

    def list_identifiers(self, person_id: str) -> list[dict]:
        result = self._get(f"/people/{person_id}/identifiers")
        return result or []
